from data import *
from utils.augmentations import SSDAugmentation
from layers.modules import MultiBoxLoss
from ssd import build_ssd
import os
import sys
import time
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.utils.data as data
import numpy as np
import argparse
import shutil

from logger import Logger
# import visdom 
# viz = visdom.Visdom()

def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Single Shot MultiBox Detector Training With Pytorch')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--dataset', default='VOC', choices=['VOC', 'COCO', 'VisDrone2018'],
                    type=str, help='VOC or COCO or VisDrone')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
                    help='Pretrained base model')
parser.add_argument('--batch_size', default=16, type=int,
                    help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('--start_iter', default=0, type=int,
                    help='Resume training at this iter')
parser.add_argument('--num_workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, type=str2bool,
                    help='Use CUDA to train model')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float,
                    help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
                    help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1, type=float,
                    help='Gamma update for SGD')
parser.add_argument('--visdom', default=False, type=str2bool,
                    help='Use visdom for loss visualization')
parser.add_argument('--save_folder', default='weights/',
                    help='Directory for saving checkpoint models')
parser.add_argument('--tensorboard', default=True, type=str2bool,
                    help='User tensorboard')
# parser.add_argument('--resolution', default=300, type=int,
#                     help='Network input resolution, [300, 512, ]')
parser.add_argument('--slowfast', default=True, type=str2bool,
                    help='Using VGG or SlowFastNetwork')
args = parser.parse_args()


if torch.cuda.is_available():
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not args.cuda:
        print("WARNING: It looks like you have a CUDA device, but aren't " +
              "using CUDA.\nRun with --cuda for optimal training speed.")
        torch.set_default_tensor_type('torch.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)


def train():

    # 选择不同的超参数配置和文件结构， 构建不同的dataset
    if args.dataset == 'COCO':
        # if args.dataset_root == VOC_ROOT:
        #     if not os.path.exists(COCO_ROOT):
        #         parser.error('Must specify dataset_root if specifying dataset')
        #     print("WARNING: Using default COCO dataset_root because " +
        #           "--dataset_root was not specified.")
        #     args.dataset_root = COCO_ROOT
        cfg = coco
        dataset = COCODetection(root=COCO_ROOT,
                                transform=SSDAugmentation(cfg['min_dim'],
                                                          cfg['means']))
    elif args.dataset == 'VOC':
        # if args.dataset_root == COCO_ROOT:
        #     parser.error('Must specify dataset if specifying dataset_root')
        cfg = voc
        dataset = VOCDetection(root=VOC_ROOT,
                               transform=SSDAugmentation(cfg['min_dim'],
                                                         cfg['means']))
    elif args.dataset == 'VisDrone2018':
        cfg = visdrone  # 选择哪一个config
        dataset = DroneDetection(root=DRONE_ROOT,
                                transform=SSDAugmentation(cfg['min_dim'],
                                                         cfg['means']))

    # if args.visdom:
    #     import visdom 
    #     viz = visdom.Visdom()

    print('num_classes: '+str(cfg['num_classes']))
    ssd_net = build_ssd('train', cfg['min_dim'], cfg['num_classes'])
    net = ssd_net

    if args.cuda:
        net = torch.nn.DataParallel(ssd_net)
        cudnn.benchmark = True

    if args.resume:
        print('Resuming training, loading {}...'.format(args.resume))
        ssd_net.load_weights(args.resume)
    # else:
    #     vgg_weights = torch.load(args.save_folder + args.basenet)
    #     print('Loading base network...')
    #     ssd_net.vgg.load_state_dict(vgg_weights)

    if args.cuda:
        net = net.cuda()

    if not args.resume:
        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        ssd_net.vgg.apply(weights_init)
        ssd_net.extras.apply(weights_init)
        ssd_net.loc.apply(weights_init)
        ssd_net.conf.apply(weights_init)

    optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum,
                          weight_decay=args.weight_decay)   # L2 penalty
    criterion = MultiBoxLoss(cfg['num_classes'], 0.5, True, 0, True, 3, 0.5,
                             False, args.cuda)

    net.train()
    # loss counters
    loc_loss = 0
    conf_loss = 0
    epoch = 0
    print('Loading the dataset...')

    epoch_size = len(dataset) // args.batch_size
    print('Training SSD on:', dataset.name)
    print('Using the specified args:')
    print(args)
    # print(args.dataset)
    step_index = 0

    if args.visdom:
        vis_title = 'SSD.PyTorch on ' + dataset.name
        vis_legend = ['Loc Loss', 'Conf Loss', 'Total Loss']
        iter_plot = create_vis_plot('Iteration', 'Loss', vis_title, vis_legend)
        epoch_plot = create_vis_plot('Epoch', 'Loss', vis_title, vis_legend)
        epoch_plot2 = create_vis_plot('Epoch', 'Loss', vis_title, vis_legend)

    if args.tensorboard:
        logger = Logger('./logs')

    # 初始化文件夹    
    with open('trainlogs.txt', 'w') as f:
        f.write('Start training on {}'.format(args.dataset))

    shutil.rmtree('args/')
    shutil.rmtree('logs/')
    os.mkdir('args/')
    os.mkdir('logs/')
    imgcnt=0

    data_loader = data.DataLoader(dataset, args.batch_size,
                                  num_workers=args.num_workers,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=True)
    # create batch iterator

    # 每个迭代向后顺序取batch size个图片
    batch_iterator = iter(data_loader)
    for iteration in range(args.start_iter, cfg['max_iter']):
        # print('it: '+str(iteration))
        if args.visdom and iteration != 0 and (iteration % epoch_size == 0):
            update_vis_plot(epoch, loc_loss, conf_loss, epoch_plot, epoch_plot2,
                            'append', epoch_size)
            # reset epoch loss counters
            loc_loss = 0
            conf_loss = 0
            epoch += 1

        if iteration in cfg['lr_steps']:
            step_index += 1
            adjust_learning_rate(optimizer, args.gamma, step_index)

        # load train data, 取数据
        # 循环一次之后iter无法回到起点，需要重新赋值
        try:
            images, targets=next(batch_iterator)
        except StopIteration:
            batch_iterator=iter(data_loader)
            images, targets=next(batch_iterator)
        # images, targets = next(batch_iterator)

        # print('feed size')
        # print(images.shape)
        # for item in targets:
        #     print(item.shape)
        if args.cuda:
            images = Variable(images.cuda())
            with torch.no_grad():
                targets = [torch.Tensor(ann.cuda()) for ann in targets]

        else:
            images = Variable(images)
            targets = [torch.Tensor(ann) for ann in targets]
        # forward
        t0 = time.time()

        # optimizer.zero_grad()

        # # output img
        # with torch.no_grad():
        #     imgtensor=torch.Tensor(images)
        #     for img in imgtensor:
        #         imgnp=np.array(img.cpu().permute(1,2,0))
        #         rgbimg=imgnp[:, :, (2, 1, 0)]
        #         cv2.imwrite('trainimg/{}_{}.jpg'.format(args.dataset, imgcnt), rgbimg)
        #         imgcnt+=1

        out = net(images)
        # backprop
        optimizer.zero_grad()   # 写在计算新的梯度之前就可以backward之前
        loss_l, loss_c = criterion(out, targets)    # 对比network output和gt
        loss = loss_l + loss_c
        # print('loss: '+str(loss_l.data)+' '+str(loss_c.data))
        loss.backward()
        optimizer.step()
        t1 = time.time()
        loc_loss += loss_l.data
        conf_loss += loss_c.data




        if iteration % 10 == 0:
            # print('timer: %.4f sec.' % (t1 - t0))
            # print('ok')
            print('iter [{}/{}]'.format(iteration, cfg['max_iter']-args.start_iter)  + ' || Loss: %.4f' % (loss.data))

            # # 打印参数
            # with open('args/args_{}.txt'.format(iteration), 'a') as f:
            #     for item in net.named_parameters():
            #         f.write(' '+str(item[0])+': '+str(item[1]))

            with open('trainlogs.txt', 'a') as f:
                f.write('iter [{}/{}]'.format(iteration, cfg['max_iter']-args.start_iter)  + ' || Loss: %.4f \n' % (loss.data))

            if args.tensorboard:
                info = {'loss': loss.data}

                for tag, value in info.items():
                    logger.scalar_summary(tag, value, iteration)

                for tag, value in net.named_parameters():
                    # print('tag: ' + str(tag))
                    # print('params: ' + str(value.data.cpu().numpy().shape)) # convert to cpu data and transform to numpy
                    tag = tag.replace('.', '/')
                    logger.histo_summary(tag, value.data.cpu().numpy(), iteration)
                    logger.histo_summary(tag+'/grad', value.grad.data.cpu().numpy(), iteration)

                info = {'images': images.view(-1, int(cfg['min_dim']), int(cfg['min_dim'])).cpu().numpy()}

                for tag, img in info.items():
                    logger.image_summary(tag, img, iteration)


            # print('iter ' + repr(iteration) + ' || Loss: %.4f ||' % (loss.data), end=' ')
        

        # print(loss)

        # print('isnan'+str(torch.isnan(loss)))
        # print(torch.isnan(loss))

        # # 检测loss爆炸
        # if torch.isnan(loss).data!=0: 
        #     print('Error')
        #     errorcnt=1
        #     with open('trainlogs.txt', 'a') as f:
        #         f.write('ERROR')
        #     # for img in images:

        #     #     cv2.imwrite('./logs/'+str(errorcnt)+'.jpg', img)
            
        #     break




            

        if args.visdom:
            update_vis_plot(iteration, loss_l.data, loss_c.data,
                            iter_plot, epoch_plot, 'append')

        if iteration != 0 and iteration % 1000 == 0:
            print('Saving state, iter:', iteration)
            torch.save(ssd_net.state_dict(), 'weights/ssd'+ str(cfg['min_dim']) +'_'+str(args.dataset)+'_' +
                       repr(iteration) + '.pth')
            with open('trainlogs.txt', 'a') as f:
                f.write('Saving state, iter:'+ str(iteration))
    torch.save(ssd_net.state_dict(),
               args.save_folder + '' + 'SSD' + str(cfg['min_dim']) + '_' + args.dataset + '.pth')


def adjust_learning_rate(optimizer, gamma, step):
    """Sets the learning rate to the initial LR decayed by 10 at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = args.lr * (gamma ** (step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def xavier(param):
    init.xavier_uniform_(param)


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        m.bias.data.zero_()


def create_vis_plot(_xlabel, _ylabel, _title, _legend):
    return viz.line(
        X=torch.zeros((1,)).cpu(),
        Y=torch.zeros((1, 3)).cpu(),
        opts=dict(
            xlabel=_xlabel,
            ylabel=_ylabel,
            title=_title,
            legend=_legend
        )
    )


def update_vis_plot(iteration, loc, conf, window1, window2, update_type,
                    epoch_size=1):
    viz.line(
        X=torch.ones((1, 3)).cpu() * iteration,
        Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu() / epoch_size,
        win=window1,
        update=update_type
    )
    # initialize epoch plot on first iteration
    if iteration == 0:
        viz.line(
            X=torch.zeros((1, 3)).cpu(),
            Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu(),
            win=window2,
            update=True
        )


if __name__ == '__main__':
    train()
