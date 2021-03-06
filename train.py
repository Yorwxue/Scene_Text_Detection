import numpy as np
import cv2
import argparse
import tensorflow as tf
import os

from craft import CRAFT

from utils.gaussian import GaussianGenerator
from utils.img_util import load_sample, img_unnormalize
from utils.loss import save_log
from utils.loss import craft_mse_loss as craft_loss
# from utils.loss import craft_mae_loss as craft_loss
# from utils.loss import craft_huber_loss as craft_loss
from utils.DataLoader import SynLoader, TTLoader, CTWLoader
from utils.box_util import reorder_points
from utils.fake_util import fake_char_boxes


parser = argparse.ArgumentParser()
parser.add_argument('--alpha', type=float, default=1., help="weight of loss of foreground")
parser.add_argument('--learning_rate', type=float, default=0.0001)  # https://github.com/clovaai/CRAFT-pytorch/issues/18
parser.add_argument('--batch_size', type=int, default=32)  # batch size for training
parser.add_argument('--canvas_size', type=int, default=768)
# parser.add_argument('--max_epochs', type=int, default=800)  # maximum number of epochs
parser.add_argument('--gpu_list', type=str, default='0')  # list of gpus to use
parser.add_argument('--use_fake', type=bool, default=False)
# path to training data
parser.add_argument('--real_data_path', type=str, default=r"./dataset/ctw/")
# parser.add_argument('--max_image_size', type=int, default=1280)
parser.add_argument('--iterations', '--iter', type=int, default=100000)
parser.add_argument('--weight_dir', type=str, default=r"./weights/", help="directory to save model weights")

args = parser.parse_args()


class DataGenerator(object):
    def __init__(self, net, train_data_dict, sample_ratio, img_size, batch_size):
        """

        Args:
            train_data_dict: dataset that include "img_path", "word_boxes", "words", "char_boxes_list" and "confidence_list"
            sample_ratio: ratio of real data(include synthetic data) and fake data
            img_size: size of input image
            batch_size:
            data_count_list: number of data for real and fake dataset respectively
            data_idx_list: index of sample data for real and fake dataset respectively
            data_mark_list: kind of data source, 0 means real data, 1 means fake data
        """
        super().__init__()
        assert len(train_data_dict.keys()) == len(sample_ratio)
        self.net = net
        self.train_data_dict = train_data_dict
        self.train_data_keys = list(self.train_data_dict.keys())
        self.sample_ratio = np.array(sample_ratio) / np.sum(sample_ratio)
        self.data_count_list = [len(train_data_dict[data_kind]) for data_kind in train_data_dict]
        self.data_idx_list = [0] * len(train_data_dict.keys())
        self.data_mark_list = list(range(len(self.train_data_keys)))
        self.img_size = img_size
        self.batch_size = batch_size
        self.iter_idx = 0

    def get_batch(self, size):
        crop_ratio = 1 + self.iter_idx // (args.iterations//5)  # from 1 to 5
        self.iter_idx += 1
        ini_min_ratio = 0.05
        ini_max_ratio = 0.2  # 0.2 * 5 == 1
        image_paths = list()
        images = list()
        word_boxes_list = list()
        word_lengths_list = list()
        region_scores = list()
        affinity_scores = list()
        confidence_score_list = list()
        fg_masks = list()
        bg_masks = list()
        gaussian_generator = GaussianGenerator()
        word_count_list = list()
        for i in range(size):
            # sample_mark: sample from real or fake dataset: 0 means real, 1 means fake
            sample_mark = np.random.choice(self.data_mark_list, p=self.sample_ratio)

            if sample_mark:  # fake
                # TODO: in process
                img_path, word_boxes, words, _, _ = self.train_data_dict[self.train_data_keys[sample_mark]][self.data_idx_list[sample_mark]]
                char_boxes_list, confidence_list = list(), list()
                for word_box, word in zip(word_boxes, words):
                    word_length = len(word)
                    char_boxes, confidence = fake_char_boxes(self.net, img_path, word_box, word_length)
                    char_boxes_list.append(char_boxes)
                    confidence_list.append(confidence)
            else:  # real
                img_path, word_boxes, words, char_boxes_list, confidence_list = self.train_data_dict[self.train_data_keys[sample_mark]][self.data_idx_list[sample_mark]]

            self.data_idx_list[sample_mark] += 1
            if self.data_idx_list[sample_mark] >= self.data_count_list[sample_mark]:
                self.data_idx_list[sample_mark] = 0
                np.random.shuffle(self.train_data_dict[self.train_data_keys[sample_mark]])

            try:
                img, word_boxes, char_boxes_list, region_box_list, affinity_box_list, img_shape = load_sample(img_path, self.img_size, word_boxes, char_boxes_list, crop_ratio=(ini_min_ratio*crop_ratio, ini_max_ratio*crop_ratio))

                images.append(img)
                image_paths.append(img_path)

                word_count = min(len(word_boxes), len(words), len(char_boxes_list))
                word_boxes = np.array(word_boxes[:word_count], dtype=np.int32) // 2
                word_boxes_list.append(word_boxes)
                word_count_list.append(word_count)

                # Note that length of characters may not equal to length of word, due to boxes of characters are split by model.
                word_lengths = [len(words[j]) if len(char_boxes_list[j]) == 0 else 0 for j in range(word_count)]
                word_lengths_list.append(word_lengths)

                height, width = img.shape[:2]
                heat_map_size = (height // 2, width // 2)

                mask_shape = (img_shape[1] // 2, img_shape[0] // 2)
                confidence_score = np.zeros(heat_map_size, dtype=np.float32)
                fg_mask = np.zeros(heat_map_size, dtype=np.uint8)
                for word_box, confidence_value in zip(word_boxes, confidence_list):
                    confidence_score_mask = np.zeros(heat_map_size, dtype=np.uint8)
                    cv2.fillPoly(confidence_score_mask, [np.array(word_box)], 1)
                    fg_mask = fg_mask + np.uint8(confidence_score_mask)
                    confidence_score_slice = np.float32(confidence_score_mask) * confidence_value
                    confidence_score = confidence_score_slice + confidence_score
                confidence_score = np.clip(confidence_score, 0, 1)
                fg_mask = np.clip(fg_mask, 0, 1)

                # fg_mask = np.zeros(heat_map_size, dtype=np.uint8)
                # cv2.fillPoly(fg_mask, [np.array(word_box) for word_box in word_boxes], 1)
                # for char_boxes in char_boxes_list:
                #     cv2.fillPoly(fg_mask, [np.array(char_box) for char_box in char_boxes], 1)

                fg_masks.append(fg_mask)
                bg_mask = np.zeros(heat_map_size, dtype=np.float32)
                bg_mask[:mask_shape[0], :mask_shape[1]] = 1
                bg_mask = bg_mask - fg_mask
                bg_mask = np.clip(bg_mask, 0, 1)
                bg_masks.append(bg_mask)

                confidence_score_list.append(np.clip(confidence_score + bg_mask, 0, 1))

                region_score = gaussian_generator.gen(heat_map_size, np.array(region_box_list) // 2)
                region_scores.append(region_score)

                affinity_score = gaussian_generator.gen(heat_map_size, np.array(affinity_box_list) // 2)
                affinity_scores.append(affinity_score)

                # show head map
                """
                from utils.img_util import to_heat_map
                from utils.box_util import reorder_points
                img_origin = cv2.cvtColor(img_unnormalize(img), cv2.COLOR_RGB2BGR)
                points_list = list()
                for char_boxes in char_boxes_list:
                    for char_box in char_boxes:
                        points = np.asarray(reorder_points(char_box), dtype=np.int)
                        points = np.reshape(points, (-1, 2))
                        points_list.append(points)
                cv2.polylines(img_origin, points_list, True, (0, 255, 255))
                img_region_heat = to_heat_map(region_score)
                img_affinity_heat = to_heat_map(affinity_score)
                img_gray_region_score = np.transpose([region_score, region_score, region_score], (1, 2, 0)) * 255
                img_confidence = np.transpose([confidence_score, confidence_score, confidence_score], (1, 2, 0)) * 255
                cv2.imwrite("example_confidence.jpg", img_confidence)
                cv2.imwrite("example.jpg", img_origin)
                cv2.imwrite("example_gray_region_heat_map.jpg", img_gray_region_score)
                cv2.imwrite("example_region_heat_map.jpg", img_region_heat)
                cv2.imwrite("example_affinity_heat_map.jpg", img_affinity_heat)
                cv2.imwrite("example_fg_mask.jpg", fg_mask*255)
                cv2.imwrite("example_bg_mask.jpg", bg_mask*255)
                print(img_path)
                # """
            except Exception as e:
                print(img_path)
                print("len(word_boxes) = ", len(word_boxes))
                print("len(char_boxes_list) = ", len(char_boxes_list))
                save_batch_images("-1", images, word_boxes, prefix="")
                print(e)
                exit()

        max_word_count = np.max(word_count_list)
        max_word_count = max(1, max_word_count)
        new_word_boxes_list = np.zeros((size, max_word_count, 4, 2), dtype=np.int32)
        new_word_lengths_list = np.zeros((size, max_word_count), dtype=np.int32)
        for i in range(size):
            if word_count_list[i] > 0:
                new_word_boxes_list[i, :word_count_list[i]] = np.array(word_boxes_list[i])
                new_word_lengths_list[i, :word_count_list[i]] = np.array(word_lengths_list[i])

        images = np.array(images)
        region_scores = np.array(region_scores, dtype=np.float32)
        affinity_scores = np.array(affinity_scores, dtype=np.float32)
        confidence_scores = np.array(confidence_score_list, dtype=np.float32)
        fg_masks = np.array(fg_masks, dtype=np.float32)
        bg_masks = np.array(bg_masks, dtype=np.float32)

        inputs = {
            "image_path": image_paths,
            "image": images,
            "word_box": new_word_boxes_list,
            "word_length": new_word_lengths_list,
            "region": region_scores,
            "affinity": affinity_scores,
            "confidence": confidence_scores,
            "fg_mask": fg_masks,
            "bg_mask": bg_masks,
        }

        return inputs


def train():
    # declare model
    net = CRAFT(input_shape=(args.canvas_size, args.canvas_size, 3))
    loss_function = craft_loss()

    # lr decay depend on https://github.com/clovaai/CRAFT-pytorch/issues/18
    lr_fn = tf.optimizers.schedules.ExponentialDecay(args.learning_rate, decay_steps=10000, decay_rate=0.8)
    optimizer = tf.keras.optimizers.Adam(lr_fn)

    checkpoint = tf.train.Checkpoint(optimizer=optimizer, model=net)
    manager = tf.train.CheckpointManager(
        checkpoint, directory=args.weight_dir, max_to_keep=10)

    # Create a checkpoint directory to store the checkpoints.
    if not os.path.exists(args.weight_dir):
        os.makedirs(args.weight_dir)
    checkpoint_dir = os.path.join(args.weight_dir, "ckpt")
    checkpoint_prefix = os.path.abspath(checkpoint_dir)

    # load dataset
    print("Data Set Loading ..")
    # train_real_data_list, test_data_list = TTLoader(args.real_data_path).get_dataset()
    train_real_data_list, test_data_list = CTWLoader(args.real_data_path).get_dataset()
    np.random.shuffle(train_real_data_list)
    np.random.shuffle(test_data_list)
    if args.use_fake:
        train_fake_data_list = []  # TODO
        np.random.shuffle(train_fake_data_list)
        train_generator = DataGenerator(net, {"real": train_real_data_list, "fake": train_fake_data_list},
                                        [5, 1], args.canvas_size, args.batch_size)
    else:
        train_generator = DataGenerator(net, {"real": train_real_data_list},
                                        [1], args.canvas_size, args.batch_size)

    print("Training Start ..")
    for idx in range(args.iterations):
        batch = train_generator.get_batch(args.batch_size)

        with tf.GradientTape() as tape:
            y, feature = net(batch["image"])
            region = y[:, :, :, 0]
            affinity = y[:, :, :, 1]
            """
            kind = "region"
            temp = batch[kind][0]
            img_temp = np.transpose([temp, temp, temp], (1, 2, 0)) * 255
            cv2.imwrite("./logs/temp_%s.jpg" % kind, img_temp)
            """
            try:
                loss, l_region, l_affinity, hard_bg_mask = loss_function([
                    batch["region"],
                    batch["affinity"],
                    region,
                    affinity,
                    batch["confidence"],
                    batch["fg_mask"],
                    batch["bg_mask"],
                    args.alpha
                ])
            except Exception as e:
                print(e)
                save_batch_images(idx, batch["image"], batch["word_box"], prefix="error_")
                loss, l_region, l_affinity, hard_bg_mask = loss_function([batch["region"], batch["affinity"], region, affinity, batch["confidence"], batch["fg_mask"], batch["bg_mask"], args.alpha])
                exit()
            if idx % 50 == 0:
                save_batch_images(idx, batch["image"], batch["word_box"])
                save_log(region, l_region, batch["region"], batch["fg_mask"], hard_bg_mask, "region", prefix="iter%d" % (idx+1))
                save_log(affinity, l_affinity, batch["affinity"], batch["fg_mask"], hard_bg_mask, "affinity", prefix="iter%d" % (idx+1))
        gradients = tape.gradient(loss, net.trainable_variables)
        optimizer.apply_gradients(zip(gradients, net.trainable_variables))
        print("iteration %d, batch loss: " % (idx+1), loss)
        # if (idx+1) % 100 == 0:
        #     checkpoint.save(checkpoint_prefix)
        manager.save()


def save_batch_images(idx, images, word_boxes, prefix="", max_keep=2):
    for batch_idx in range(np.min([len(images), max_keep])):
        img = images[batch_idx]
        iter_idx = "iter%d" % (idx + 1)
        display = (img - np.min(img)) / (np.max(img) - np.min(img)) * 255
        display = cv2.resize(display, ((np.shape(display)[0] // 2), (np.shape(display)[1] // 2)))

        points_list = list()
        for word_box in word_boxes[batch_idx]:
            points = np.asarray(word_box, dtype=np.int)
            points = np.reshape(points, (-1, 2))
            points_list.append(points)
        cv2.polylines(display, points_list, True, (0, 255, 255))
        cv2.imwrite("./logs/%s%s_%d_img.jpg" % (prefix, iter_idx, batch_idx), display)


if __name__ == '__main__':
    train()
