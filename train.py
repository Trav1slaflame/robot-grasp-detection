''' 
Training a network on cornell grasping dataset for detecting grasping positions.
'''
import sys
import os.path
import glob
import torch
import torch.utils.data
import numpy as np
# from logger import Logger
import time
# import img_preproc
from bbox_utils import *
from models.model_utils import *
from opts import opts
# from shapely.geometry import Polygon
from data.grasp_data import GraspDataset
import cv2
import tensorboardX
import datetime
import os
import argparse
import logging
from .data import get_dataset
from models.ResNet50 import resnet_50
import torch.optim as optim
from torchsummary import summary
from models.common import post_process_output
from dataset_processing import evaluation


# import tensorflow as tf

def validate(net, device, val_data, batches_per_epoch):
    """
    Run validation.
    :param net: Network
    :param device: Torch device
    :param val_data: Validation Dataset
    :param batches_per_epoch: Number of batches to run
    :return: Successes, Failures and Losses
    """
    net.eval()

    results = {
        'correct': 0,
        'failed': 0,
        'loss': 0,
        'losses': {

        }
    }

    ld = len(val_data)

    with torch.no_grad():
        batch_idx = 0
        while batch_idx < batches_per_epoch:
            for x, y, didx, rot, zoom_factor in val_data:
                batch_idx += 1
                if batches_per_epoch is not None and batch_idx >= batches_per_epoch:
                    break

                xc = x.to(device)
                yc = [yy.to(device) for yy in y]
                lossd = net.compute_loss(xc, yc)

                loss = lossd['loss']

                results['loss'] += loss.item()/ld
                for ln, l in lossd['losses'].items():
                    if ln not in results['losses']:
                        results['losses'][ln] = 0
                    results['losses'][ln] += l.item()/ld

                q_out, ang_out, w_out = post_process_output(lossd['pred']['pos'], lossd['pred']['cos'],
                                                            lossd['pred']['sin'], lossd['pred']['width'])

                s = evaluation.calculate_iou_match(q_out, ang_out,
                                                   val_data.dataset.get_gtbb(didx, rot, zoom_factor),
                                                   no_grasps=1,
                                                   grasp_width=w_out,
                                                   )

                if s:
                    results['correct'] += 1
                else:
                    results['failed'] += 1

    return results


DATA_PATH = '../datasets/cornell_grasping_dataset/data-1'
ANN_PATH = '../datasets/cornell_grasping_dataset/annotations/train.json'


def train():
    opt = opts()
    # logger = Logger(opt)

    print('Creating model...')
    model = create_model()   # creates the graspnet model
    
    CGD_DATASET = GraspDataset(DATA_PATH, ANN_PATH)
    # images, bboxes = img_preproc.distorted_inputs([data_files_], FLAGS.num_epochs, batch_size=FLAGS.batch_size)

    train_loader = torch.utils.data.DataLoader(
        dataset=CGD_DATASET,
        batch_size=1,
        shuffle=True,
        num_workers=0,
        pin_memory=True,
        drop_last=False
    )


    for i_batch, sample_batched in enumerate(train_loader):
        print(i_batch)
        # print(type(sample_batched[1][0]))
        # print(sample_batched[1][0:8])
        for i in range(0,len(sample_batched[1]),8):
            x, y, tan, h, w = bboxes_to_grasps(sample_batched[1][i:i+8])
            # print(x,y,tan,w,h)

        # observe one batch and stop.
        if i_batch == 0:
            break


    if torch.cuda.is_available():
        print('CUDA is available: {}'.format(torch.cuda.is_available()))
        model = torch.nn.DataParallel(model).cuda()
    else:
        model = torch.nn.DataParallel(model)

    model.training = True


def run():
    args = parse_args()

    # Vis window
    if args.vis:
        cv2.namedWindow('Display', cv2.WINDOW_NORMAL)

    # Set-up output directories
    dt = datetime.datetime.now().strftime('%y%m%d_%H%M')
    net_desc = '{}_{}'.format(dt, '_'.join(args.description.split()))

    save_folder = os.path.join(args.outdir, net_desc)
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)
    tb = tensorboardX.SummaryWriter(os.path.join(args.logdir, net_desc))

    # Load Dataset
    logging.info('Loading {} Dataset...'.format(args.dataset.title()))
    Dataset = get_dataset(args.dataset)

    train_dataset = Dataset(args.dataset_path, start=0.0, end=args.split, ds_rotate=args.ds_rotate,
                            random_rotate=True, random_zoom=True,
                            include_depth=args.use_depth, include_rgb=args.use_rgb)
    train_data = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers
    )
    val_dataset = Dataset(args.dataset_path, start=args.split, end=1.0, ds_rotate=args.ds_rotate,
                          random_rotate=True, random_zoom=True,
                          include_depth=args.use_depth, include_rgb=args.use_rgb)
    val_data = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=args.num_workers
    )
    logging.info('Done')

    # Load the network
    logging.info('Loading Network...')
    input_channels = 3*args.use_rgb             #  1*args.use_depth + 3*args.use_rgb
    # ggcnn = get_network(args.network)
    net = resnet_50                                      #   ggcnn(input_channels=input_channels)
    device = torch.device("cuda:0")
    net = net.to(device)
    optimizer = optim.Adam(net.parameters())
    logging.info('Done')

    # Print model architecture.
    summary(net, (input_channels, 224, 224))
    f = open(os.path.join(save_folder, 'arch.txt'), 'w')
    sys.stdout = f
    summary(net, (input_channels, 224, 224))
    sys.stdout = sys.__stdout__
    f.close()

    best_iou = 0.0
    for epoch in range(args.epochs):
        logging.info('Beginning Epoch {:02d}'.format(epoch))
        train_results = train(epoch, net, device, train_data, optimizer, args.batches_per_epoch, vis=args.vis)

        # Log training losses to tensorboard
        tb.add_scalar('loss/train_loss', train_results['loss'], epoch)
        for n, l in train_results['losses'].items():
            tb.add_scalar('train_loss/' + n, l, epoch)

        # Run Validation
        logging.info('Validating...')
        test_results = validate(net, device, val_data, args.val_batches)
        logging.info('%d/%d = %f' % (test_results['correct'], test_results['correct'] + test_results['failed'],
                                     test_results['correct']/(test_results['correct']+test_results['failed'])))

        # Log validation results to tensorbaord
        tb.add_scalar('loss/IOU', test_results['correct'] / (test_results['correct'] + test_results['failed']), epoch)
        tb.add_scalar('loss/val_loss', test_results['loss'], epoch)
        for n, l in test_results['losses'].items():
            tb.add_scalar('val_loss/' + n, l, epoch)

        # Save best performing network
        iou = test_results['correct'] / (test_results['correct'] + test_results['failed'])
        if iou > best_iou or epoch == 0 or (epoch % 10) == 0:
            torch.save(net, os.path.join(save_folder, 'epoch_%02d_iou_%0.2f' % (epoch, iou)))
            torch.save(net.state_dict(), os.path.join(save_folder, 'epoch_%02d_iou_%0.2f_statedict.pt' % (epoch, iou)))
            best_iou = iou


if __name__ == '__main__':
    run()
    
#     x, y, tan, h, w = bboxes_to_grasps(bboxes)
#     x_hat, y_hat, tan_hat, h_hat, w_hat = torch.unbind(model(images), axis=1) # list

#     # tangent of 85 degree is 11 
#     tan_hat_confined = torch.minimum(11., torch.maximum(-11., tan_hat))
#     tan_confined = torch.minimum(11., torch.maximum(-11., tan))

#     # Loss function
#     gamma = tf.constant(10.)
#     loss = torch.sum(torch.pow(x_hat -x, 2) +torch.pow(y_hat -y, 2) + gamma*torch.pow(tan_hat_confined - tan_confined, 2) +torch.pow(h_hat -h, 2) +torch.pow(w_hat -w, 2))
#     train_op = tf.train.AdamOptimizer(epsilon=0.1).minimize(loss)
#     init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
#     sess = tf.Session()
#     sess.run(init_op)
#     coord = tf.train.Coordinator()
#     threads = tf.train.start_queue_runners(sess=sess, coord=coord)

#     #save/restore model
#     d={}
#     l = ['w1', 'b1', 'w2', 'b2', 'w3', 'b3', 'w4', 'b4', 'w5', 'b5', 'w_fc1', 'b_fc1', 'w_fc2', 'b_fc2']
#     for i in l:
#         d[i] = [v for v in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES) if v.name == i+':0'][0]
    
#     dg={}
#     lg = ['w1', 'b1', 'w2', 'b2', 'w3', 'b3', 'w4', 'b4', 'w5', 'b5', 'w_fc1', 'b_fc1', 'w_fc2', 'b_fc2', 'w_output', 'b_output']
#     for i in lg:
#         dg[i] = [v for v in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES) if v.name == i+':0'][0]

#     saver = tf.train.Saver(d)
#     saver_g = tf.train.Saver(dg)
#     #saver.restore(sess, "/root/grasp/grasp-detection/models/imagenet/m2/m2.ckpt")
#     saver_g.restore(sess, FLAGS.model_path)
#     try:
#         count = 0
#         step = 0
#         start_time = time.time()
#         while not coord.should_stop():
#             start_batch = time.time()
#             #train
#             if FLAGS.train_or_validation == 'train':
#                 _, loss_value, x_value, x_model, tan_value, tan_model, h_value, h_model, w_value, w_model = sess.run([train_op, loss, x, x_hat, tan, tan_hat, h, h_hat, w, w_hat])
#                 duration = time.time() - start_batch
#                 if step % 100 == 0:             
#                     print('Step %d | loss = %s\n | x = %s\n | x_hat = %s\n | tan = %s\n | tan_hat = %s\n | h = %s\n | h_hat = %s\n | w = %s\n | w_hat = %s\n | (%.3f sec/batch\n')%(step, loss_value, x_value[:3], x_model[:3], tan_value[:3], tan_model[:3], h_value[:3], h_model[:3], w_value[:3], w_model[:3], duration)
#                 if step % 1000 == 0:
#                     saver_g.save(sess, FLAGS.model_path)
#             else:
#                 bbox_hat = grasp_to_bbox(x_hat, y_hat, tan_hat, h_hat, w_hat)
#                 bbox_value, bbox_model, tan_value, tan_model = sess.run([bboxes, bbox_hat, tan, tan_hat])
#                 bbox_value = np.reshape(bbox_value, -1)
#                 bbox_value = [(bbox_value[0]*0.35,bbox_value[1]*0.47),(bbox_value[2]*0.35,bbox_value[3]*0.47),(bbox_value[4]*0.35,bbox_value[5]*0.47),(bbox_value[6]*0.35,bbox_value[7]*0.47)] 
#                 p1 = Polygon(bbox_value)
#                 p2 = Polygon(bbox_model)
#                 iou = p1.intersection(p2).area / (p1.area +p2.area -p1.intersection(p2).area) 
#                 angle_diff = np.abs(np.arctan(tan_model)*180/np.pi -np.arctan(tan_value)*180/np.pi)
#                 duration = time.time() -start_batch
#                 if angle_diff < 30. and iou >= 0.25:
#                     count+=1
#                     print('image: %d | duration = %.2f | count = %d | iou = %.2f | angle_difference = %.2f' %(step, duration, count, iou, angle_diff))
#             step +=1
#     except tf.errors.OutOfRangeError:
#         print('Done training for %d epochs, %d steps, %.1f min.' % (FLAGS.num_epochs, step, (time.time()-start_time)/60))
#     finally:
#         coord.request_stop()

#     coord.join(threads)
#     sess.close()

# def run_epoch(epoch, model, data_loader):
#     model.train()



    
if __name__ == '__main__':
    train()