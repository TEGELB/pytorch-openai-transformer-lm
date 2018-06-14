import re
import os
import time
import math
import json
import joblib
import random
import argparse
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from tqdm import tqdm
from functools import partial
from sklearn.utils import shuffle
from sklearn.metrics import accuracy_score

from model_py import Model, LMHead, ClfHead, load_openai_pretrained_model
from opt import adam, warmup_cosine, warmup_linear, warmup_constant
from datasets import rocstories
from analysis import rocstories as rocstories_analysis
from text_utils import TextEncoder
from utils import (encode_dataset, flatten, iter_data,
                   ResultLogger, make_path)

OPT_FNS = {
    'adam':adam,
}

LR_SCHEDULES = {
    'warmup_cosine':warmup_cosine,
    'warmup_linear':warmup_linear,
    'warmup_constant':warmup_constant,
}

class LossCompute:
    "A Loss compute and train function."
    def __init__(self, lm_criterion, clf_criterion, lm_coef):
        self.lm_criterion = lm_criterion
        self.clf_criterion = clf_criterion
        self.lm_coef = lm_coef

    def __call__(self, X, Y, M, lm_logits, clf_logits):
        # Language modeling loss
        x_shifted = X[:, :, 1:, 0].contiguous().view(-1)           # Shape: 252
        M = M.view(-1, M.size(2))
        lm_losses = self.lm_criterion(lm_logits, x_shifted)
        lm_losses = lm_losses.view(X.size(0) * X.size(1), X.size(2)-1)
        lm_losses = lm_losses * M[:, 1:]
        lm_losses = lm_losses.sum(1) / torch.sum(M[:, 1:], 1)

        # Classification loss
        clf_losses = self.clf_criterion(clf_logits, Y)
        if self.lm_coef > 0:
            train_loss = clf_losses.sum() + self.lm_coef * lm_losses.sum()
        else:
            train_loss = clf_losses.sum()
        return train_loss

def transform_roc(X1, X2, X3):
    n_batch = len(X1)
    xmb = np.zeros((n_batch, 2, n_ctx, 2), dtype=np.int32)
    mmb = np.zeros((n_batch, 2, n_ctx), dtype=np.float32)
    start = encoder['_start_']
    delimiter = encoder['_delimiter_']
    for i, (x1, x2, x3), in enumerate(zip(X1, X2, X3)):
        x12 = [start]+x1[:max_len]+[delimiter]+x2[:max_len]+[clf_token]
        x13 = [start]+x1[:max_len]+[delimiter]+x3[:max_len]+[clf_token]
        l12 = len(x12)
        l13 = len(x13)
        xmb[i, 0, :l12, 0] = x12
        xmb[i, 1, :l13, 0] = x13
        mmb[i, 0, :l12] = 1
        mmb[i, 1, :l13] = 1
    xmb[:, :, :, 1] = np.arange(n_vocab+n_special, n_vocab+n_special+n_ctx)
    return xmb, mmb

# def iter_apply(Xs, Ms, Ys):
#     fns = [lambda x:np.concatenate(x, 0), lambda x:float(np.sum(x))]
#     results = []
#     for xmb, mmb, ymb in iter_data(Xs, Ms, Ys, n_batch=n_batch_train, truncate=False, verbose=True):
#         n = len(xmb)
#         if n == n_batch_train:
#             res = sess.run([eval_mgpu_logits, eval_mgpu_clf_loss], {X_train:xmb, M_train:mmb, Y_train:ymb})
#         else:
#             res = sess.run([eval_logits, eval_clf_loss], {X:xmb, M:mmb, Y:ymb})
#         res = [r*n for r in res]
#         results.append(res)
#     results = zip(*results)
#     return [fn(res) for res, fn in zip(results, fns)]

# def iter_predict(Xs, Ms):
#     logits = []
#     for xmb, mmb in iter_data(Xs, Ms, n_batch=n_batch_train, truncate=False, verbose=True):
#         n = len(xmb)
#         if n == n_batch_train:
#             logits.append(sess.run(eval_mgpu_logits, {X_train:xmb, M_train:mmb}))
#         else:
#             logits.append(sess.run(eval_logits, {X:xmb, M:mmb}))
#     logits = np.concatenate(logits, 0)
#     return logits

# def log():
#     global best_score
#     tr_logits, tr_cost = iter_apply(trX[:n_valid], trM[:n_valid], trY[:n_valid])
#     va_logits, va_cost = iter_apply(vaX, vaM, vaY)
#     tr_cost = tr_cost/len(trY[:n_valid])
#     va_cost = va_cost/n_valid
#     tr_acc = accuracy_score(trY[:n_valid], np.argmax(tr_logits, 1))*100.
#     va_acc = accuracy_score(vaY, np.argmax(va_logits, 1))*100.
#     logger.log(n_epochs=n_epochs, n_updates=n_updates, tr_cost=tr_cost, va_cost=va_cost, tr_acc=tr_acc, va_acc=va_acc)
#     print('%d %d %.3f %.3f %.2f %.2f'%(n_epochs, n_updates, tr_cost, va_cost, tr_acc, va_acc))
#     if submit:
#         score = va_acc
#         if score > best_score:
#             best_score = score
#             save(os.path.join(save_dir, desc, 'best_params.jl'))

# def predict():
#     filename = filenames[dataset]
#     pred_fn = pred_fns[dataset]
#     label_decoder = label_decoders[dataset]
#     predictions = pred_fn(iter_predict(teX, teM))
#     if label_decoder is not None:
#         predictions = [label_decoder[prediction] for prediction in predictions]
#     path = os.path.join(submission_dir, filename)
#     os.makedirs(os.path.dirname(path), exist_ok=True)
#     with open(path, 'w') as f:
#         f.write('{}\t{}\n'.format('index', 'prediction'))
#         for i, prediction in enumerate(predictions):
#             f.write('{}\t{}\n'.format(i, prediction))

argmax = lambda x:np.argmax(x, 1)

pred_fns = {
    'rocstories':argmax,
}

filenames = {
    'rocstories':'ROCStories.tsv',
}

label_decoders = {
    'rocstories':None,
}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desc', type=str)
    parser.add_argument('--dataset', type=str)
    parser.add_argument('--log_dir', type=str, default='log/')
    parser.add_argument('--save_dir', type=str, default='save/')
    parser.add_argument('--data_dir', type=str, default='data/')
    parser.add_argument('--submission_dir', type=str, default='submission/')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--analysis', action='store_true')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n_iter', type=int, default=3)
    parser.add_argument('--n_batch', type=int, default=8)
    parser.add_argument('--max_grad_norm', type=int, default=1)
    parser.add_argument('--lr', type=float, default=6.25e-5)
    parser.add_argument('--lr_warmup', type=float, default=0.002)
    parser.add_argument('--n_ctx', type=int, default=512)
    parser.add_argument('--n_embd', type=int, default=768)
    parser.add_argument('--n_head', type=int, default=12)
    parser.add_argument('--n_layer', type=int, default=12)
    parser.add_argument('--embd_pdrop', type=float, default=0.1)
    parser.add_argument('--attn_pdrop', type=float, default=0.1)
    parser.add_argument('--resid_pdrop', type=float, default=0.1)
    parser.add_argument('--clf_pdrop', type=float, default=0.1)
    parser.add_argument('--l2', type=float, default=0.01)
    parser.add_argument('--vector_l2', action='store_true')
    parser.add_argument('--n_gpu', type=int, default=4)
    parser.add_argument('--opt', type=str, default='adam')
    parser.add_argument('--afn', type=str, default='gelu')
    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--encoder_path', type=str, default='model/encoder_bpe_40000.json')
    parser.add_argument('--bpe_path', type=str, default='model/vocab_40000.bpe')
    parser.add_argument('--n_transfer', type=int, default=12)
    parser.add_argument('--lm_coef', type=float, default=0.5)
    parser.add_argument('--b1', type=float, default=0.9)
    parser.add_argument('--b2', type=float, default=0.999)
    parser.add_argument('--e', type=float, default=1e-8)

    args = parser.parse_args()
    print(args)
    globals().update(args.__dict__) #TODO remove gobal
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    # torch.device object used throughout this script TODO add gpu setting
    device = torch.device("cpu") #"cuda" if use_cuda else "cpu")

    logger = ResultLogger(path=os.path.join(log_dir, '{}.jsonl'.format(desc)), **args.__dict__)
    text_encoder = TextEncoder(encoder_path, bpe_path)
    encoder = text_encoder.encoder
    n_vocab = len(text_encoder.encoder)

    (trX1, trX2, trX3, trY), (vaX1, vaX2, vaX3, vaY), (teX1, teX2, teX3) = encode_dataset(rocstories(data_dir), encoder=text_encoder)
    n_y = 2
    encoder['_start_'] = len(encoder)
    encoder['_delimiter_'] = len(encoder)
    encoder['_classify_'] = len(encoder)
    clf_token = encoder['_classify_']
    n_special = 3
    max_len = n_ctx//2-2
    n_ctx = min(
                max(
                    [len(x1[:max_len])+max(len(x2[:max_len]), len(x3[:max_len])) for x1, x2, x3 in zip(trX1, trX2, trX3)]
                    +[len(x1[:max_len])+max(len(x2[:max_len]), len(x3[:max_len])) for x1, x2, x3 in zip(vaX1, vaX2, vaX3)]
                    +[len(x1[:max_len])+max(len(x2[:max_len]), len(x3[:max_len])) for x1, x2, x3 in zip(teX1, teX2, teX3)]
                   )+3, n_ctx
                )
    vocab = n_vocab + n_special + n_ctx
    trX, trM = transform_roc(trX1, trX2, trX3)
    vaX, vaM = transform_roc(vaX1, vaX2, vaX3)
    if submit:
        teX, teM = transform_roc(teX1, teX2, teX3)

    n_train = len(trY)
    n_valid = len(vaY)
    n_batch_train = n_batch*n_gpu
    n_updates_total = (n_train//n_batch_train)*n_iter

    model = Model(vocab, args)
    lm_head = LMHead(model, args)
    clf_head = ClfHead(clf_token, args)
    compute_loss = LossCompute(nn.CrossEntropyLoss(reduce=False), nn.CrossEntropyLoss(reduce=False), lm_coef) # TODO check loss functions
    # TODO Initialize model (?)
    # TODO add train() and eval()
    load_openai_pretrained_model(model, n_ctx, n_special, args)

    model.to(device)
    lm_head.to(device)
    clf_head.to(device)

    n_updates = 0
    n_epochs = 0
    if dataset != 'stsb':
        trYt = trY
    if submit:
        path = os.path.join(save_dir, desc, 'best_params')
        torch.save(model.state_dict(), make_path(path))

    best_score = 0
    for i in range(n_iter):
        for xmb, mmb, ymb in iter_data(*shuffle(trX, trM, trYt, random_state=np.random),
                                       n_batch=n_batch_train, truncate=True, verbose=True):
            XMB = torch.tensor(xmb, dtype=torch.long).to(device)
            YMB = torch.tensor(ymb, dtype=torch.long).to(device)
            MMB = torch.tensor(mmb).to(device)
            model.train()
            h = model(XMB)
            lm_logits = lm_head(h)
            clf_logits = clf_head(h, XMB)
            loss = compute_loss(XMB, YMB, MMB, lm_logits, clf_logits)
            loss.backward()
            
            n_updates += 1
            #if n_updates in [1000, 2000, 4000, 8000, 16000, 32000] and n_epochs == 0:
                # log()
        n_epochs += 1
        # log()
    # if submit:
    #     sess.run([p.assign(ip) for p, ip in zip(params, joblib.load(os.path.join(save_dir, desc, 'best_params.jl')))])
    #     predict()
    #     if analysis:
    #         rocstories_analysis(data_dir, os.path.join(submission_dir, 'ROCStories.tsv'), os.path.join(log_dir, 'rocstories.jsonl'))