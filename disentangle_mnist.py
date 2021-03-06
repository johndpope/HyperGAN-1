import os
import sys
import time
import argparse
import numpy as np
import pprint
import torch

from torch import nn
from torch import optim
from torch.nn import functional as F

import ops
import utils
import netdef
import datagen


def load_args():

    parser = argparse.ArgumentParser(description='param-wgan')
    parser.add_argument('--z', default=400, type=int, help='latent space width')
    parser.add_argument('--ze', default=256, type=int, help='encoder dimension')
    parser.add_argument('--batch_size', default=32, type=int)
    parser.add_argument('--epochs', default=200000, type=int)
    parser.add_argument('--target', default='small2', type=str)
    parser.add_argument('--dataset', default='mnist', type=str)
    parser.add_argument('--beta', default=1000, type=int)
    parser.add_argument('--resume', default=False, type=bool)
    parser.add_argument('--use_x', default=False, type=bool)
    parser.add_argument('--load_e', default=False, type=bool)
    parser.add_argument('--pretrain_e', default=False, type=bool)
    parser.add_argument('--scratch', default=False, type=bool)
    parser.add_argument('--exp', default='0', type=str)
    parser.add_argument('--use_d', default=False, type=str)
    parser.add_argument('--use_aux', default=False, type=str)
    parser.add_argument('--model', default='small', type=str)

    args = parser.parse_args()
    return args


class AuxDz(nn.Module):
    def __init__(self, args):
        super(AuxDz, self).__init__()
        for k, v in vars(args).items():
            setattr(self, k, v)
        
        self.name = 'AuxDz'
        self.linear1 = nn.Linear(self.z//10, 512)
        self.linear2 = nn.Linear(512, 1024)
        self.linear3 = nn.Linear(1024, 1024)
        self.linear4 = nn.Linear(1024, 10)
        self.relu = nn.ELU(inplace=True)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # print ('AuxDz in: ', x.shape)
        x = x.view(self.batch_size, -1)
        x = self.relu(self.linear1(x))
        x = self.relu(self.linear2(x))
        x = self.relu(self.linear3(x))
        x = self.linear4(x)
        # print ('AuxDz out: ', x.shape)
        return x


# hard code the two layer net
def train_clf(args, Z, data, target, val=False):
    """ calc classifier loss """
    data, target = data.cuda(), target.cuda()
    out = F.conv2d(data, Z[0], stride=1)
    out = F.leaky_relu(out)
    out = F.max_pool2d(out, 2, 2)
    out = F.conv2d(out, Z[1], stride=1)
    out = F.leaky_relu(out)
    out = F.max_pool2d(out, 2, 2)
    out = out.view(-1, 512)
    out = F.linear(out, Z[2])
    loss = F.cross_entropy(out, target)
    correct = None
    if val:
        pred = out.data.max(1, keepdim=True)[1]
        correct = pred.eq(target.data.view_as(pred)).long().cpu().sum()
    return (correct, loss)


def train(args):
    
    torch.manual_seed(8734)
    
    netE = models.Encoder(args).cuda()
    W1 = models.GeneratorW1(args).cuda()
    W2 = models.GeneratorW2(args).cuda()
    W3 = models.GeneratorW3(args).cuda()
    netD = models.DiscriminatorZ(args).cuda()
    Aux = AuxDz(args).cuda()
    print (netE, W1, W2, W3, Aux)#netD)

    optimE = optim.Adam(netE.parameters(), lr=.0005, betas=(0.5, 0.9), weight_decay=1e-4)
    optimW1 = optim.Adam(W1.parameters(), lr=5e-4, betas=(0.5, 0.9), weight_decay=1e-4)
    optimW2 = optim.Adam(W2.parameters(), lr=5e-4, betas=(0.5, 0.9), weight_decay=1e-4)
    optimW3 = optim.Adam(W3.parameters(), lr=5e-4, betas=(0.5, 0.9), weight_decay=1e-4)
    optimD = optim.Adam(netD.parameters(), lr=5e-5, betas=(0.5, 0.9), weight_decay=1e-4)
    optimAux = optim.Adam(Aux.parameters(), lr=5e-5, betas=(0.5, 0.9), weight_decay=1e-4)
    
    best_test_acc, best_test_loss = 0., np.inf
    args.best_loss, args.best_acc = best_test_loss, best_test_acc

    mnist_train, mnist_test = datagen.load_mnist(args)
    x_dist = utils.create_d(args.ze)
    z_dist = utils.create_d(args.z)
    one = torch.FloatTensor([1]).cuda()
    mone = (one * -1).cuda()
    print ("==> pretraining encoder")
    j = 0
    final = 100.
    e_batch_size = 1000
    if args.load_e:
        netE, optimE, _ = utils.load_model(args, netE, optimE)
        print ('==> loading pretrained encoder')
    if args.pretrain_e:
        for j in range(2000):
            x = utils.sample_d(x_dist, e_batch_size)
            z = utils.sample_d(z_dist, e_batch_size)
            codes = netE(x)
            for i, code in enumerate(codes):
                code = code.view(e_batch_size, args.z)
                mean_loss, cov_loss = ops.pretrain_loss(code, z)
                loss = mean_loss + cov_loss
                loss.backward(retain_graph=True)
            optimE.step()
            netE.zero_grad()
            print ('Pretrain Enc iter: {}, Mean Loss: {}, Cov Loss: {}'.format(
                j, mean_loss.item(), cov_loss.item()))
            final = loss.item()
            if loss.item() < 0.1:
                print ('Finished Pretraining Encoder')
                break

    print ('==> Begin Training')
    for _ in range(1000):
        for batch_idx, (data, target) in enumerate(mnist_train):
            ops.batch_zero_grad([netE, W1, W2, W3, netD])
            ops.batch_zero_grad([optimE, optimW1, optimW2, optimW3, optimAux])
            z = utils.sample_d(x_dist, args.batch_size)
            codes = netE(z)
            l1 = W1(codes[0]).mean(0)
            l2 = W2(codes[1]).mean(0)
            l3 = W3(codes[2]).mean(0)

            if args.use_aux:
                #free_params([Aux])
                #frozen_params([netE, W1, W2, W3])
                # just do the last conv layer
                # split latent space into chunks -- each representing a class
                factors = torch.split(codes[1], args.z//10, 1)
                for y, factor in enumerate(factors):
                    target = (torch.ones(args.batch_size, dtype=torch.long) * y).cuda()
                    aux_pred = Aux(factor)
                    aux_loss = F.cross_entropy(aux_pred, target)
                    aux_loss.backward(retain_graph=True)
                optimAux.step()
                #frozen_params([Aux])
                #free_params([netE, W1, W2, W3])
            
            correct, loss = train_clf(args, [l1, l2, l3], data, target, val=True)
            scaled_loss = args.beta*loss
            scaled_loss.backward()
            optimE.step()
            optimW1.step()
            optimW2.step()
            optimW3.step()
            loss = loss.item()
                
            if batch_idx % 50 == 0:
                acc = (correct / 1) 
                print ('**************************************')
                print ('MNIST Test, beta: {}'.format(args.beta))
                print ('Acc: {}, Loss: {}'.format(acc, loss))
                print ('best test loss: {}'.format(args.best_loss))
                print ('best test acc: {}'.format(args.best_acc))
                print ('**************************************')

            if batch_idx % 200 == 0:
                test_acc = 0.
                test_loss = 0.
                for i, (data, y) in enumerate(mnist_test):
                    z = utils.sample_d(x_dist, args.batch_size)
                    codes = netE(z)
                    l1 = W1(codes[0]).mean(0)
                    l2 = W2(codes[1]).mean(0)
                    l3 = W3(codes[2]).mean(0)
                    min_loss_batch = 10.
                    correct, loss = train_clf(args, [l1, l2, l3], data, y, val=True)
                    test_acc += correct.item()
                    test_loss += loss.item()
                test_loss /= len(mnist_test.dataset)
                test_acc /= len(mnist_test.dataset)
                print ('Test Accuracy: {}, Test Loss: {}'.format(test_acc, test_loss))
                if test_loss < best_test_loss or test_acc > best_test_acc:
                    print ('==> new best stats, saving')
                    if test_loss < best_test_loss:
                        best_test_loss = test_loss
                        args.best_loss = test_loss
                    if test_acc > best_test_acc:
                        best_test_acc = test_acc
                        args.best_acc = test_acc


if __name__ == '__main__':

    args = load_args()
    if args.model == 'small':
        import models.models_mnist_small as models
    elif args.model == 'nobn':
        import models.models_mnist_nobn as models
    elif args.model == 'full':
        import models.models_mnist as models
    else:
        raise NotImplementedError

    modeldef = netdef.nets()[args.target]
    pprint.pprint (modeldef)
    # log some of the netstat quantities so we don't subscript everywhere
    args.stat = modeldef
    args.shapes = modeldef['shapes']
    train(args)
