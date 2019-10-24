import os
import json
import torch
from torchvision import transforms
import numpy as np
from PIL import Image
from lib.utils.zipreader import ZipReader
import cv2


def get_convert_label_fn(odgt):
    """
    A function that converts labels to expected range [-1, num_classes-1] where -1 is ignored.
    When using custom dataset, you might want to add your own function.
    """

    def convert_ade_label(segm):
        "Convert ADE labels to range [-1, 149]"
        return segm - 1

    def convert_cityscapes_label(segm):
        "Convert cityscapes labels to range [-1, 18]"
        ignore_label = -1
        label_mapping = {
            -1: ignore_label, 0: ignore_label,
            1: ignore_label, 2: ignore_label,
            3: ignore_label, 4: ignore_label,
            5: ignore_label, 6: ignore_label,
            7: 0, 8: 1, 9: ignore_label,
            10: ignore_label, 11: 2, 12: 3,
            13: 4, 14: ignore_label, 15: ignore_label,
            16: ignore_label, 17: 5, 18: ignore_label,
            19: 6, 20: 7, 21: 8, 22: 9, 23: 10, 24: 11,
            25: 12, 26: 13, 27: 14, 28: 15,
            29: ignore_label, 30: ignore_label,
            31: 16, 32: 17, 33: 18}

        temp = segm.clone()
        for k, v in label_mapping.items():
            segm[temp == k] = v
        return segm

    if 'cityscapes' in odgt.lower():
        return convert_cityscapes_label
    elif 'ade' in odgt.lower():
        return convert_ade_label
    else:
        return lambda x: x


def imresize(im, size, interp='bilinear'):
    if interp == 'nearest':
        resample = PIL.Image.NEAREST
    elif interp == 'bilinear':
        resample = PIL.Image.BILINEAR
    elif interp == 'bicubic':
        resample = PIL.Image.BICUBIC
    else:
        raise Exception('resample method undefined!')

    return np.array(
        PIL.Image.fromarray(im).resize((size[1], size[0]), resample)
    )


class BaseDataset(torch.utils.data.Dataset):
    def __init__(self, odgt, opt, **kwargs):
        # parse options
        self.imgSizes = opt.imgSizes
        self.imgMaxSize = opt.imgMaxSize
        # max down sampling rate of network to avoid rounding during conv or pooling
        self.padding_constant = opt.padding_constant

        # parse the input list
        self.parse_input_list(odgt, **kwargs)

        # mean and std
        self.normalize = transforms.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225])
        self.convert_label = get_convert_label_fn(odgt)

    def parse_input_list(self, odgt, max_sample=-1, start_idx=-1, end_idx=-1):
        if isinstance(odgt, list):
            self.list_sample = odgt
        elif isinstance(odgt, str):
            self.list_sample = [json.loads(x.rstrip()) for x in open(odgt, 'r')]

        if max_sample > 0:
            self.list_sample = self.list_sample[0:max_sample]
        if start_idx >= 0 and end_idx >= 0:  # divide file list
            self.list_sample = self.list_sample[start_idx:end_idx]

        self.num_sample = len(self.list_sample)
        assert self.num_sample > 0
        print('# samples: {}'.format(self.num_sample))

    def img_transform(self, img):
        # 0-255 to 0-1
        img = np.float32(img) / 255.
        img = img.transpose((2, 0, 1))
        img = self.normalize(torch.from_numpy(img.copy()))
        return img

    def segm_transform_ade(self, segm):
        # to tensor, -1 to 149
        segm = torch.from_numpy(segm).long() - 1
        return segm

    def segm_transform_citi(self, segm):
        # transform segm label to tensor
        segm = torch.from_numpy(segm).long()
        # convert/map labels to expected range
        segm = self.convert_label(segm)
        return segm

    # Round x to the nearest multiple of p and x' >= x
    def round2nearest_multiple(self, x, p):
        return ((x - 1) // p + 1) * p


class TrainDataset(BaseDataset):
    def __init__(self, root_dataset, odgt, opt, batch_per_gpu=1, **kwargs):
        super(TrainDataset, self).__init__(odgt, opt, **kwargs)
        self.root_dataset = root_dataset
        # down sampling rate of segm labe
        self.segm_downsampling_rate = opt.segm_downsampling_rate
        self.batch_per_gpu = batch_per_gpu

        # classify images into two classes: 1. h > w and 2. h <= w
        self.batch_record_list = [[], []]

        # override dataset length when trainig with batch_per_gpu > 1
        self.cur_idx = 0
        self.if_shuffled = False
        self.odgt = odgt

    def _get_sub_batch(self):
        while True:
            # get a sample record
            this_sample = self.list_sample[self.cur_idx]
            if this_sample['height'] > this_sample['width']:
                self.batch_record_list[0].append(this_sample) # h > w, go to 1st class
            else:
                self.batch_record_list[1].append(this_sample) # h <= w, go to 2nd class

            # update current sample pointer
            self.cur_idx += 1
            if self.cur_idx >= self.num_sample:
                self.cur_idx = 0
                np.random.shuffle(self.list_sample)

            if len(self.batch_record_list[0]) == self.batch_per_gpu:
                batch_records = self.batch_record_list[0]
                self.batch_record_list[0] = []
                break
            elif len(self.batch_record_list[1]) == self.batch_per_gpu:
                batch_records = self.batch_record_list[1]
                self.batch_record_list[1] = []
                break
        return batch_records

    def __getitem__(self, index):
        # NOTE: random shuffle for the first time. shuffle in __init__ is useless
        if not self.if_shuffled:
            np.random.seed(index)
            np.random.shuffle(self.list_sample)
            self.if_shuffled = True

        # get sub-batch candidates
        batch_records = self._get_sub_batch()

        # resize all images' short edges to the chosen size
        if isinstance(self.imgSizes, list) or isinstance(self.imgSizes, tuple):
            this_short_size = np.random.choice(self.imgSizes)
        else:
            this_short_size = self.imgSizes

        # calculate the BATCH's height and width
        # since we concat more than one samples, the batch's h and w shall be larger than EACH sample
        batch_widths = np.zeros(self.batch_per_gpu, np.int32)
        batch_heights = np.zeros(self.batch_per_gpu, np.int32)
        for i in range(self.batch_per_gpu):
            img_height, img_width = batch_records[i]['height'], batch_records[i]['width']
            this_scale = min(
                this_short_size / min(img_height, img_width), \
                self.imgMaxSize / max(img_height, img_width))
            batch_widths[i] = img_width * this_scale
            batch_heights[i] = img_height * this_scale

        # Here we must pad both input image and segmentation map to size h' and w' so that p | h' and p | w'
        batch_width = np.max(batch_widths)
        batch_height = np.max(batch_heights)
        batch_width = int(self.round2nearest_multiple(batch_width, self.padding_constant))
        batch_height = int(self.round2nearest_multiple(batch_height, self.padding_constant))

        assert self.padding_constant >= self.segm_downsampling_rate, \
            'padding constant must be equal or large than segm downsamping rate'
        batch_images = torch.zeros(
            self.batch_per_gpu, 3, batch_height, batch_width)
        batch_segms = torch.zeros(
            self.batch_per_gpu,
            batch_height // self.segm_downsampling_rate,
            batch_width // self.segm_downsampling_rate).long()

        for i in range(self.batch_per_gpu):
            this_record = batch_records[i]

            # load image and label
            image_path = self.root_dataset+'ADEChallengeData2016.zip@/ADEChallengeData2016'+this_record['fpath_img'].lstrip('ADEChallengeData2016')
            segm_path = self.root_dataset+'ADEChallengeData2016.zip@/ADEChallengeData2016'+this_record['fpath_segm'].lstrip('ADEChallengeData2016')
            if 'cityscapes' in self.odgt.lower():
                image_path = self.root_dataset+'leftImg8bit_trainvaltest.zip@/leftImg8bit/'\
                +'/'.join(this_record['fpath_img'].split('/')[2:])
                segm_path = self.root_dataset+'gtFine_trainvaltest.zip@/gtFine/'\
                +'/'.join(this_record['fpath_segm'].split('/')[2:])

            img = ZipReader.imread(image_path, 'BGR')
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            segm = ZipReader.imread(segm_path, 'P')
            #assert(segm.mode == "L")
            assert(img.shape[0] == segm.shape[0])
            assert(img.shape[1] == segm.shape[1])

            # random_flip
            flip = np.random.choice(2) * 2 - 1
            img = img[:, ::flip, :]
            segm = segm[:, ::flip]

            # note that each sample within a mini batch has different scale param
            img = cv2.resize(img, (batch_widths[i], batch_heights[i]), cv2.INTER_LINEAR)
            segm = cv2.resize(segm, (batch_widths[i], batch_heights[i]), cv2.INTER_NEAREST)

            # further downsample seg label, need to avoid seg label misalignment
            segm_rounded_height = self.round2nearest_multiple(segm.shape[0], self.segm_downsampling_rate)
            segm_rounded_width = self.round2nearest_multiple(segm.shape[1], self.segm_downsampling_rate)
            segm_rounded = np.zeros((segm_rounded_height, segm_rounded_width), dtype='uint8')
            segm_rounded[:segm.shape[0], :segm.shape[1]] = segm
            segm = cv2.resize(
                segm_rounded,
                (segm_rounded.shape[1] // self.segm_downsampling_rate, \
                 segm_rounded.shape[0] // self.segm_downsampling_rate), \
                cv2.INTER_NEAREST)

            # image transform, to torch float tensor 3xHxW
            img = self.img_transform(img)

            # segm transform, to torch long tensor HxW
            if 'cityscapes' in self.odgt.lower():
                segm = self.segm_transform_citi(segm)
            elif 'ade' in self.odgt.lower():
                segm = self.segm_transform_ade(segm)
            else:
                print('Dataset unrecognized')
            # put into batch arrays
            batch_images[i][:, :img.shape[1], :img.shape[2]] = img
            batch_segms[i][:segm.shape[0], :segm.shape[1]] = segm

        output = dict()
        output['img_data'] = batch_images
        output['seg_label'] = batch_segms
        return output

    def __len__(self):
        return int(1e10) # It's a fake length due to the trick that every loader maintains its own list
        #return self.num_sampleclass


class ValDataset(BaseDataset):
    def __init__(self, root_dataset, odgt, opt, **kwargs):
        super(ValDataset, self).__init__(odgt, opt, **kwargs)
        self.root_dataset = root_dataset

    def __getitem__(self, index):
        this_record = self.list_sample[index]
        # load image and label
        image_path = self.root_dataset + 'ADEChallengeData2016.zip@/ADEChallengeData2016' + this_record['fpath_img'].lstrip('ADEChallengeData2016')
        segm_path = self.root_dataset + 'ADEChallengeData2016.zip@/ADEChallengeData2016' + this_record['fpath_segm'].lstrip('ADEChallengeData2016')
        elif 'cityscapes' in self.odgt.lower():
            image_path = self.root_dataset + 'leftImg8bit_trainvaltest.zip@/leftImg8bit/' \
                         + '/'.join(this_record['fpath_img'].split('/')[2:])
            segm_path = self.root_dataset + 'gtFine_trainvaltest.zip@/gtFine/' \
                        + '/'.join(this_record['fpath_segm'].split('/')[2:])
        img = ZipReader.imread(image_path, 'BGR')
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        segm = ZipReader.imread(segm_path, 'P')

        assert(img.shape[0] == segm.shape[0])
        assert(img.shape[1] == segm.shape[1])

        ori_width, ori_height = img.shape[1], img.shape[0]

        img_resized_list = []
        for this_short_size in self.imgSizes:
            # calculate target height and width
            scale = min(this_short_size / float(min(ori_height, ori_width)),
                        self.imgMaxSize / float(max(ori_height, ori_width)))
            target_height, target_width = int(ori_height * scale), int(ori_width * scale)

            # to avoid rounding in network
            target_width = self.round2nearest_multiple(target_width, self.padding_constant)
            target_height = self.round2nearest_multiple(target_height, self.padding_constant)

            # resize images
            img_resized = cv2.resize(img, (target_width, target_height), cv2.INTER_LINEAR)

            # image transform, to torch float tensor 3xHxW
            img_resized = self.img_transform(img_resized)
            img_resized = torch.unsqueeze(img_resized, 0)
            img_resized_list.append(img_resized)

        # segm transform, to torch long tensor HxW
        segm = self.segm_transform(segm)
        batch_segms = torch.unsqueeze(segm, 0)

        output = dict()
        output['img_ori'] = np.array(img)
        output['img_data'] = [x.contiguous() for x in img_resized_list]
        output['seg_label'] = batch_segms.contiguous()
        output['info'] = this_record['fpath_img']
        return output

    def __len__(self):
        return self.num_sample

class TestDataset(BaseDataset):
    def __init__(self, odgt, opt, **kwargs):
        super(TestDataset, self).__init__(odgt, opt, **kwargs)

    def __getitem__(self, index):
        this_record = self.list_sample[index]
        # load image
        image_path = this_record['fpath_img']
        img = Image.open(image_path).convert('RGB')

        ori_width, ori_height = img.size

        img_resized_list = []
        for this_short_size in self.imgSizes:
            # calculate target height and width
            scale = min(this_short_size / float(min(ori_height, ori_width)),
                        self.imgMaxSize / float(max(ori_height, ori_width)))
            target_height, target_width = int(ori_height * scale), int(ori_width * scale)

            # to avoid rounding in network
            target_width = self.round2nearest_multiple(target_width, self.padding_constant)
            target_height = self.round2nearest_multiple(target_height, self.padding_constant)

            # resize images
            img_resized = imresize(img, (target_width, target_height), interp='bilinear')

            # image transform, to torch float tensor 3xHxW
            img_resized = self.img_transform(img_resized)
            img_resized = torch.unsqueeze(img_resized, 0)
            img_resized_list.append(img_resized)

        output = dict()
        output['img_ori'] = np.array(img)
        output['img_data'] = [x.contiguous() for x in img_resized_list]
        output['info'] = this_record['fpath_img']
        return output

    def __len__(self):
        return self.num_sample
