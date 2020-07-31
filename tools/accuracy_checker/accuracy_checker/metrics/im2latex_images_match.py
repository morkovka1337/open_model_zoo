"""
Copyright (c) 2019 Intel Corporation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import argparse
import cv2 as cv
import logging
import numpy as np
import os
import re
import shlex
import shutil
import subprocess
import sys
from multiprocessing import Pool, cpu_count
from multiprocessing.dummy import Pool as ThreadPool
from threading import Timer
import tempfile


from ..config import PathField, NumberField
from .metric import FullDatasetEvaluationMetric
from ..representation import CharacterRecognitionAnnotation, CharacterRecognitionPrediction

MAX_PX_ROW_DIFF = 3
TIMEOUT = 10

# replace \pmatrix with \begin{pmatrix}\end{pmatrix}
# replace \matrix with \begin{matrix}\end{matrix}
template = r"""
\documentclass[12pt]{article}
\pagestyle{empty}
\usepackage{amsmath}
\newcommand{\mymatrix}[1]{\begin{matrix}#1\end{matrix}}
\newcommand{\mypmatrix}[1]{\begin{pmatrix}#1\end{pmatrix}}
\begin{document}
\begin{displaymath}
%s
\end{displaymath}
\end{document}
"""


def crop_image(img, output_path, default_size=None):
    old_im = cv.imread(img, cv.IMREAD_GRAYSCALE)
    img_data = np.copy(old_im)
    nnz_inds = np.where(img_data != 255)
    if len(nnz_inds[0]) == 0:
        if not default_size:
            cv.imwrite(output_path, old_im)
            return False
        else:
            assert len(default_size) == 2, default_size
            x_min, y_min, x_max, y_max = 0, 0, default_size[0], default_size[1]
            old_im = old_im[y_min: y_max + 1, x_min, x_max + 1]
            cv.imwrite(output_path, old_im)
            return False
    y_min = np.min(nnz_inds[0])
    y_max = np.max(nnz_inds[0])
    x_min = np.min(nnz_inds[1])
    x_max = np.max(nnz_inds[1])

    old_im = old_im[y_min: y_max + 1, x_min: x_max + 1]
    cv.imwrite(output_path, old_im)
    return True


def run(cmd, timeout_sec):
    proc = subprocess.Popen(cmd, shell=True)
    kill_proc = lambda p: p.kill()
    timer = Timer(timeout_sec, kill_proc, [proc])
    try:
        timer.start()
        stdout, stderr = proc.communicate()
    finally:
        timer.cancel()


def preprocess_formula(l):
    """Formula preprocessing

    Args:
        l (str): Input formula

    Returns:
        str: Preprocessed formula
    """
    l = l.strip()
    l = l.replace(r'\pmatrix', r'\mypmatrix')
    l = l.replace(r'\matrix', r'\mymatrix')
    # remove leading comments
    l = l.strip('%')
    if len(l) == 0:
        l = '\\hspace{1cm}'
    # \hspace {1 . 5 cm} -> \hspace {1.5cm}
    for space in ["hspace", "vspace"]:
        match = re.finditer(space + " {(.*?)}", l)
        if match:
            new_l = ""
            last = 0
            for m in match:
                new_l = new_l + l[last:m.start(1)] + m.group(1).replace(" ", "")
                last = m.end(1)
            new_l = new_l + l[last:]
            l = new_l
    return l


def render_routine(line):
    """Function for rendering single formula

    Args:
        line (tuple): formula idx, formula string, path to store rendered image
    """
    formula, file_idx, folder_path = line
    output_path = os.path.join(folder_path, file_idx)
    pre_name = output_path.replace('/', '_').replace('.', '_')
    formula = preprocess_formula(formula)
    if not os.path.exists(output_path):
        tex_filename = pre_name + '.tex'
        log_filename = pre_name + '.log'
        aux_filename = pre_name + '.aux'
        with open(tex_filename, "w") as w:
            w.write(template % formula)
        run("pdflatex -interaction=nonstopmode {}  >/dev/null".format(tex_filename), TIMEOUT)
        os.remove(tex_filename)
        os.remove(log_filename)
        os.remove(aux_filename)
        pdf_filename = tex_filename[:-4] + '.pdf'
        png_filename = tex_filename[:-4] + '.png'
        if not os.path.exists(pdf_filename):
            logging.info('ERROR: {} cannot compile\n'.format(file_idx))
        else:
            os.system("convert -density 200 -quality 100 %s %s" % (pdf_filename, png_filename))
            os.remove(pdf_filename)
            if os.path.exists(png_filename):
                crop_image(png_filename, output_path)
                os.remove(png_filename)


def match_images(params):
    """Function for single comparing two images

    Args:
        im1 (str): path to image1
        im2 (str): path to image2
        out_path (str, optional): Path to store diff of two images. Defaults to None.
        max_pixel_column_diff (int, optional): Maximum number of black pixels in column
        to treat it as whitespaced column. Defaults to 0.

    Returns:
        Tuple of booleans: match with space (as is), match without space
    """
    im1, im2, out_path, max_pixel_column_diff = params
    im1 = cv.imread(im1, cv.IMREAD_GRAYSCALE)
    im2 = cv.imread(im2, cv.IMREAD_GRAYSCALE)
    if im2 is None:
        # image 2 not rendered
        return False, False

    def check_differ(diff):
        """Checks if difference of two images has a substring
        of blue or red pixels with length >= MAX_PX_ROW_DIFF
        In other words, if one image is shifted from another less then
        MAX_PX_ROW_DIFF, images are equal

        Args:
            diff (np.array): Difference of two images

        Returns:
            bool: Images match
        """
        for row in np.transpose(diff, (1, 0, 2)):
            for px_idx in range(len(row) - MAX_PX_ROW_DIFF):
                if (row[px_idx: px_idx + MAX_PX_ROW_DIFF] == ((255, 0, 0),) * MAX_PX_ROW_DIFF).all() or \
                        (row[px_idx: px_idx + MAX_PX_ROW_DIFF] == ((0, 0, 255),) * MAX_PX_ROW_DIFF).all():
                    return True
        return False

    def preprocess(im1):
        img_data1 = np.asarray(im1, dtype=np.uint8)  # height, width

        # transpose for more convinient work
        img_data1 = np.transpose(img_data1)

        img_data1 = (img_data1 >= 160).astype(np.uint8)
        return img_data1

    img_data1 = preprocess(im1)
    img_data2 = preprocess(im2)
    w1, h1 = img_data1.shape[0:2]
    w2, h2 = img_data2.shape[0:2]

    max_h = max(h1, h2)
    max_w = max(w1, w2)
    padded_im_1 = np.ones((max_w, max_h))
    padded_im_2 = np.ones((max_w, max_h))
    padded_im_1[0:img_data1.shape[0], 0:img_data1.shape[1]] = img_data1
    padded_im_2[0:img_data2.shape[0], 0:img_data2.shape[1]] = img_data2
    if (padded_im_1 == padded_im_2).all():
        return True, True

    # check if difference realy is (e.g. it is not shift on 1-2 px)
    diff = np.zeros((*padded_im_1.shape, 3), dtype=np.uint8)
    diff[(padded_im_1 == 1) * (padded_im_2 == 1), :] = (255, 255, 255)
    diff[(padded_im_1 == 1) * (padded_im_2 == 0), :] = (255, 0, 0)
    diff[(padded_im_1 == 0) * (padded_im_2 == 1), :] = (0, 0, 255)

    differ = check_differ(diff)
    if differ:
        # create color map of differences with spaces
        cv.imwrite(out_path.replace('.png', '_with_s.png'),
                   np.transpose(diff, (1, 0, 2)))
    else:
        return True, True
    # remove whitespace colmuns and evaluate images again
    spaceless_im_1 = np.array(
        [column for column in padded_im_1 if sum(column == 0) > max_pixel_column_diff])
    spaceless_im_2 = np.array(
        [column for column in padded_im_2 if sum(column == 0) > max_pixel_column_diff])
    if max(spaceless_im_1.shape) == 0 or max(spaceless_im_2.shape) == 0:
        return False, False
    if spaceless_im_1.shape == spaceless_im_2.shape and (spaceless_im_1 == spaceless_im_2).all():
        return False, True

    max_h = max(spaceless_im_1.shape[1], spaceless_im_2.shape[1])
    max_w = max(spaceless_im_1.shape[0], spaceless_im_2.shape[0])
    diff = np.zeros((max_w, max_h))
    mask1 = ((spaceless_im_1 == 1) * 2)
    mask2 = ((spaceless_im_2 == 1) * 3)
    diff[0: mask1.shape[0], 0:mask1.shape[1]] += mask1
    diff[0: mask2.shape[0], 0:mask2.shape[1]] += mask2

    new_diff = np.zeros((*diff.shape, 3), dtype=np.uint8)
    new_diff[diff == 2] = (255, 0, 0)
    new_diff[diff == 3] = (0, 0, 255)
    new_diff[diff == 5] = (255, 255, 255)

    differ = check_differ(new_diff)
    if differ:
        cv.imwrite(out_path.replace('.png', '_wout_s.png'),
                   np.transpose(new_diff, (1, 0, 2)))
        return False, False
    return False, True


class Im2latexRenderBasedMetric(FullDatasetEvaluationMetric):
    __provider__ = 'im2latex_match_images_metric'
    annotation_types = (CharacterRecognitionAnnotation, )
    prediction_types = (CharacterRecognitionPrediction, )

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'num_threads': NumberField(value_type=int, optional=True),
            'max_pixel_column_diff': NumberField(value_type=int)
        })

        return parameters

    def configure(self):
        self.num_threads = self.get_value_from_config('num_threads')
        if self.num_threads is None:
            self.num_threads = cpu_count()
        self.max_pixel_column_diff = self.get_value_from_config('max_pixel_column_diff')

    def compare_pics(self, images_dir):
        """
        Function reads images and compares them, first, as is
        second, deletes all whitespaces and compares again.
        This step helps to escape the situation when two pics
        are not equal because of different length of spaces
        """
        total_num = 0
        total_correct = 0
        total_correct_eliminate = 0
        lines = []
        pool = ThreadPool(self.num_threads)
        gold_dir = os.path.join(images_dir, 'images_gold')
        pred_dir = os.path.join(images_dir, 'images_pred')
        plots_dir = os.path.join(images_dir, 'diff')
        filenames = os.listdir(gold_dir)
        if not os.path.exists(plots_dir):
            os.makedirs(plots_dir)
        for filename in filenames:
            filename = os.path.join(gold_dir, filename)
            filename2 = os.path.join(pred_dir, os.path.basename(filename))
            plotfilename = os.path.join(plots_dir, os.path.basename(filename))
            lines.append((filename, filename2, plotfilename,
                          self.max_pixel_column_diff))
        results = pool.map(match_images, lines)
        assert len(results) == len(lines)
        for element in results:

            match1, match2 = element
            total_num += 1
            if match1:
                total_correct += 1
            if match2:
                total_correct_eliminate += 1

        correct_ratio = float(total_correct / total_num)
        correct_eliminate_ratio = float(total_correct_eliminate / total_num)
        logging.info('------------------------------------')
        logging.info('Final')
        logging.info('Total Num: {}'.format(total_num))
        logging.info('Accuracy (w spaces): {}'.format(correct_ratio))
        logging.info('Accuracy (w/o spaces): {}'.format(correct_eliminate_ratio))
        logging.info('Total Correct (w spaces): {}'.format(total_correct))
        logging.info('Total Correct (w/o spaces): {}'.format(total_correct_eliminate))
        return correct_eliminate_ratio

    def render_images(self, annotations, predictions, images_dir):
        """Runs render script to render images and store them into images_dir

        Args:
            annotations (str): Ground-truth formula
            predictions (str): Predicted formula
        """
        out_path_gold = os.path.join(images_dir, 'images_gold')
        out_path_pred = os.path.join(images_dir, 'images_pred')
        for dir_ in [out_path_gold, out_path_pred]:
            if not os.path.exists(dir_):
                os.makedirs(dir_)
        lines_gold = [(ann.label, ann.identifier, out_path_gold) for ann in annotations]
        lines_pred = [(pred.label, pred.identifier, out_path_pred) for pred in predictions]
        lines = lines_gold + lines_pred
        logging.info('Creating pool with {} threads'.format(self.num_threads))
        pool = ThreadPool(self.num_threads)
        logging.info('Jobs running...')
        results = pool.map(render_routine, lines)
        pool.close()
        pool.join()

    def evaluate(self, annotations, predictions):
        result = 0
        with tempfile.TemporaryDirectory() as images_dir:
            self.render_images(annotations, predictions, images_dir)
            result = self.compare_pics(images_dir)
        return result
