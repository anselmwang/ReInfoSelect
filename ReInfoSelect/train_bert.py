import argparse

import numpy as np
import torch
from torch import nn, optim
from torch.autograd import Variable
import torch.nn.functional as F

from transformers import *

from policy import all_policy
from dataloaders import *
from models import BertForRanking
from metrics import *

def dev(args, model, dev_data, device):
    rst_dict = {}
    for s, batch in enumerate(dev_data):
        query_id = batch[0]
        doc_id = batch[1]
        qd_score = batch[2]
        batch = tuple(t.to(device) for t in batch[3:])
        (raw_score, d_input_ids, d_input_mask, d_segment_ids) = batch

        with torch.no_grad():
            doc_scores, _ = model(d_input_ids, d_input_mask, d_segment_ids, raw_score)
        d_scores = doc_scores.detach().cpu().tolist()

        for (q_id, d_id, qd_s, d_s) in zip(query_id, doc_id, qd_score, d_scores):
            if q_id in rst_dict:
                rst_dict[q_id].append((qd_s, d_s, d_id))
            else:
                rst_dict[q_id] = [(qd_s, d_s, d_id)]

    with open(args.res, 'w') as writer:
        for q_id, scores in rst_dict.items():
            res = sorted(scores, key=lambda x: x[1], reverse=True)
            for rank, value in enumerate(res):
                writer.write(q_id+' '+'Q0'+' '+str(value[2])+' '+str(rank+1)+' '+str(value[1])+' bert\n')

    m_ndcg = ndcg(args.qrels, args.res, args.depth)
    m_err = err(args.qrels, args.res, args.depth)
    measure = [m_ndcg, m_err]
    return measure

def train(args, policy, p_optim, model, m_optim, crit, word2vec, tokenizer, dev_data, device):
    best_ndcg = 0.0
    for i_episode in range(args.epoch):
        # train data
        train_data = bert_train_dataloader(args, word2vec, tokenizer)
        ndcg, err = dev(args, model, dev_data, device)
        if ndcg > best_ndcg:
            best_ndcg = ndcg
        last_ndcg = ndcg

        log_prob_ps = []
        log_prob_ns = []
        log_probs = []
        rewards = []
        for step, batch in enumerate(train_data):
            # select action
            batch = tuple(t.to(device) for t in batch)
            (query_idx, doc_idx, query_len, doc_len, p_input_ids, p_input_mask, p_segment_ids, n_input_ids, n_input_mask, n_segment_ids) = batch

            probs = policy(query_idx, pos_idx, query_len, pos_len)
            dist  = Categorical(probs)
            action = dist.sample()
            if action.sum().item() < 1 and step % args.T != 0:
                continue

            mask = action.ge(0.5)
            weights = Variable(action, requires_grad=False).cuda()
            log_prob_p = dist.log_prob(action)
            log_prob_n = dist.log_prob(1-action)
            log_prob_ps.append(torch.masked_select(log_prob_p, mask))
            log_prob_ns.append(torch.masked_select(log_prob_n, mask))

            p_scores, _ = model(p_input_ids, p_segment_ids, p_input_mask)
            n_scores, _ = model(n_input_ids, n_segment_ids, n_input_mask)
            label = torch.ones(p_scores.size()).to(device)
            batch_loss = crit(p_scores, n_scores, Variable(label, requires_grad=False))
            batch_loss = batch_loss.mul(weights).mean()
            batch_loss.backward()
            m_optim.step()
            m_optim.zero_grad()

            ndcg, err = dev(args, model, dev_data, device)
            if ndcg > best_ndcg:
                best_ndcg = ndcg
            reward = ndcg - last_ndcg
            last_ndcg = ndcg
            rewards.append(reward)

            if len(rewards) > 0 and step % args.T == 0:
                R = 0.0
                policy_loss = []
                returns = []
                for ri in reversed(range(len(rewards))):
                    R = rewards[ri] + gamma * R
                    if R > 0:
                        log_probs.insert(0, log_prob_ps[ri])
                        returns.insert(0, R)
                    else:
                        log_probs.insert(0, log_prob_ns[ri])
                        returns.insert(0, -R)
                for lp, r in zip(log_probs, returns):
                    policy_loss.append((-lp * r).sum().unsqueeze(-1))
                loss = torch.cat(policy_loss).sum()
                loss.backward()
                p_optim.step()
                p_optim.zero_grad()

                log_prob_ps = []
                log_prob_ns = []
                log_probs = []
                rewards = []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-task', choices=['ClueWeb09', 'Robust04', 'ClueWeb12'], default='ClueWeb09')
    parser.add_argument('-train', default='./data/weak_supervision.tsv')
    parser.add_argument('-dev', default='./data/ClueWeb09/dev.tsv')
    parser.add_argument('-qrels', default='./data/ClueWeb09/qrels')
    parser.add_argument('-embed', default='./data/glove.6B.300d.txt')
    parser.add_argument('-vocab_size', default=400002)
    parser.add_argument('-embed_dim', default=300)
    parser.add_argument('-res', default='./out.trec')
    parser.add_argument('-depth', default=20)
    parser.add_argument('-gamma', default=0.99)
    parser.add_argument('-T', default=4)
    parser.add_argument('-n_kernels', default=21)
    parser.add_argument('-max_seq_len', default=128)
    parser.add_argument('-epoch', default=1)
    parser.add_argument('-batch_size', default=4)
    args = parser.parse_args()

    # init embedding
    word2vec, embedding_init = embloader(args)
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # init policy
    policy = all_policy(args, embedding_init)
    policy.to(device)
    p_optim = torch.optim.Adam(filter(lambda p: p.requires_grad, policy.parameters()), lr=1e-4)

    # init model
    model = BertForRanking()
    model.to(device)

    # init optimizer and load dev_data
    optimizer_grouped_parameters = [{'params': [], 'weight_decay': 0.0}]
    param_optimizer = list(model.named_parameters())
    for n, p in param_optimizer:
        optimizer_grouped_parameters[0]['params'].append(p)
    m_optim = AdamW(optimizer_grouped_parameters, lr=5e-5)
    dev_data = bert_dev_dataloader(args, tokenizer)

    # loss function
    crit = nn.MarginRankingLoss(margin=1, size_average=True)
    crit.to(device)

    if torch.cuda.device_count() > 1:
        policy = nn.DataParallel(policy)
        model = nn.DataParallel(model)
        crit = nn.DataParallel(crit)

    train(args, policy, p_optim, model, m_optim, crit, word2vec, tokenizer, dev_data, device)

if __name__ == "__main__":
    main()
