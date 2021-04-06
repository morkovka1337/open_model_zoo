"""
Copyright (c) 2018-2021 Intel Corporation

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
import lmdb
import numpy as np
import cv2

from .format_converter import DirectoryBasedAnnotationConverter, ConverterReturn
from ..representation import CharacterRecognitionAnnotation
from ..logging import print_info


class LMDBConverter(DirectoryBasedAnnotationConverter):
    __provider__ = 'lmdb_database'
    annotation_types = (CharacterRecognitionAnnotation, )

    @classmethod
    def parameters(cls):
        configuration_parameters = super().parameters()

        return configuration_parameters

    def configure(self):
        super().configure()

    def convert(self, check_content=False, progress_callback=None, progress_interval=100, **kwargs):
        """Reads data from disk and returns dataset in converted for AC format

        Args:
            check_content (bool, optional): Check if content is valid. Defaults to False.
            progress_callback (bool, optional): Display progress. Defaults to None.
            progress_interval (int, optional): Units to display progress. Defaults to 100 (percent).

        Returns:
            [type]: Converted dataset
        """
        annotations = []
        content_errors = None if not check_content else []
        lmdb_env = lmdb.open(bytes(self.data_dir), readonly=True)
        with lmdb_env.begin(write=False) as txn:
            num_iterations = int(txn.get('num-samples'.encode()))
            for index in range(1, num_iterations + 1):
                label_key = 'label-%09d'.encode() % index
                text = txn.get(label_key).decode('utf-8')

                # identifier = cv2.imdecode(imgbuf, cv2.IMREAD_ANYCOLOR)
                if progress_callback is not None and index % progress_interval == 0:
                    progress_callback(index / num_iterations * 100)
                if check_content:
                    img_key = 'image-%09d'.encode() % index
                    image_bytes = txn.get(img_key)
                    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_ANYCOLOR)
                    if image is None:
                        content_errors.append('{}: does not exist'.format('image-%09d' % index))
                annotations.append(CharacterRecognitionAnnotation(index, text))

        return ConverterReturn(annotations, None, content_errors)
