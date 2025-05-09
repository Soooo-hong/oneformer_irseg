# ------------------------------------------------------------------------------
# Reference: https://github.com/facebookresearch/Mask2Former/blob/main/mask2former/data/dataset_mappers/coco_instance_new_baseline_dataset_mapper.py
# Modified by Jitesh Jain (https://github.com/praeclarumjj3)
# ------------------------------------------------------------------------------

import copy
import logging

import numpy as np
import torch
import cv2
from detectron2.data import MetadataCatalog
from detectron2.config import configurable
from detectron2.structures import Instances, Boxes, BitMasks
from detectron2.structures import BoxMode
from detectron2.data import detection_utils as utils
from detectron2.data import transforms as T
from detectron2.data.transforms import BigCopyPasteAugmentation
from oneformer.data.tokenizer import SimpleTokenizer, Tokenize
from pycocotools import mask as coco_mask
from pycocotools.coco import COCO

__all__ = ["InstanceCOCOCustomNewBaselineDatasetMapper"]


def convert_coco_poly_to_mask(segmentations, height, width):
    masks = []
    for polygons in segmentations:
        rles = coco_mask.frPyObjects(polygons, height, width)
        mask = coco_mask.decode(rles)
        if len(mask.shape) < 3:
            mask = mask[..., None]
        mask = torch.as_tensor(mask, dtype=torch.uint8)
        mask = mask.any(dim=2)
        masks.append(mask)
    if masks:
        masks = torch.stack(masks, dim=0)
    else:
        masks = torch.zeros((0, height, width), dtype=torch.uint8)
    return masks

def convert_coco_poly_to_mask_(segmentations, height, width):
    masks_ = []
    masks = []
    for polygons in segmentations:
        
        if isinstance(polygons, list) and all(isinstance(p, float) for p in polygons):
            masks_.append(polygons)
        
        elif isinstance(polygons, list) and all(isinstance(p, list) for p in polygons):
            # already list of list
            masks_.extend(polygons)
        elif isinstance(polygons, np.ndarray):
            masks_.append(polygons.tolist())
        else:
            raise ValueError(f"Unexpected polygon format: {type(polygons)}")
        
        rles = coco_mask.frPyObjects(masks_, height, width)
        mask = coco_mask.decode(rles)
        if len(mask.shape) < 3:
            mask = mask[..., None]
        mask = torch.as_tensor(mask, dtype=torch.uint8)
        mask = mask.any(dim=2)
        if mask.shape != (height, width):
            # 자동 resize
            mask = torch.from_numpy(cv2.resize(mask.numpy().astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST))

        masks.append(mask)
    if len(masks) == 1: 
        masks = np.expand_dims(masks[0], axis=0)
    elif masks:
        masks = torch.stack(masks, dim=0)
    else:
        masks = torch.zeros((0, height, width), dtype=torch.uint8)
    return masks

def binary_masks_to_polygons(masks, tolerance=1.0):
    """
    Convert binary masks to COCO polygon format.

    Args:
        masks: list of binary masks (H, W) or a tensor (N, H, W)
        tolerance: approximation tolerance for contour (higher → simpler polygons)

    Returns:
        List of polygons (as list[list[float]])
    """
    polygons_list = []

    for mask_np in masks:
        if isinstance(mask_np, torch.Tensor):
            mask_np = mask_np.cpu().numpy()
        if mask_np.ndim == 3:
            mask_np = mask_np.squeeze()
        if mask_np.dtype != np.uint8:
            mask_np = mask_np.astype(np.uint8)

        mask_np = np.asfortranarray(mask_np.astype(np.uint8))
        contours, _ = cv2.findContours(mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        polygons = []
        for contour in contours:
            contour = contour.squeeze(1)  # shape: (N, 2)
            if len(contour) < 3:
                continue
            # polygon = contour.flatten().tolist()
            polygon = [float(x) for x in contour.flatten()]
            # COCO requires all polygons to have even length and >=6
            if len(polygon) >= 6:
                polygons.append(polygon)

        polygons_list.append(polygons)

    return polygons_list

def build_transform_gen(cfg, is_train):
    """
    Create a list of default :class:`Augmentation` from config.
    Now it includes resizing and flipping.
    Returns:
        list[Augmentation]
    """
    assert is_train, "Only support training augmentation"
    image_size = cfg.INPUT.IMAGE_SIZE
    min_scale = cfg.INPUT.MIN_SCALE
    max_scale = cfg.INPUT.MAX_SCALE

    augmentation = []

    if cfg.INPUT.RANDOM_FLIP != "none":
        augmentation.append(
            T.RandomFlip(
                horizontal=cfg.INPUT.RANDOM_FLIP == "horizontal",
                vertical=cfg.INPUT.RANDOM_FLIP == "vertical",
            )
        )

    augmentation.extend([
        T.ResizeScale(
            min_scale=min_scale, max_scale=max_scale, target_height=image_size, target_width=image_size
        ),
        T.FixedSizeCrop(crop_size=(image_size, image_size)),
    ])
    return augmentation


# This is specifically designed for the COCO Instance Segmentation dataset.
class InstanceCOCOCustomNewBaselineDatasetMapper:
    """
    A callable which takes a dataset dict in Detectron2 Dataset format,
    and map it into a format used by OneFormer for custom instance segmentation using COCO format.

    The callable currently does the following:

    1. Read the image from "file_name"
    2. Applies geometric transforms to the image and annotation
    3. Find and applies suitable cropping to the image and annotation
    4. Prepare image and annotation to Tensors
    """

    @configurable
    def __init__(
        self,
        is_train=True,
        *,
        num_queries,
        tfm_gens,
        meta,
        image_format,
        max_seq_len,
        task_seq_len,
    ):
        """
        NOTE: this interface is experimental.
        Args:
            is_train: for training or inference
            augmentations: a list of augmentations or deterministic transforms to apply
            crop_gen: crop augmentation
            tfm_gens: data augmentation
            image_format: an image format supported by :func:`detection_utils.read_image`.
        """
        self.tfm_gens = tfm_gens
        logging.getLogger(__name__).info(
            "[InstanceCOCOCustomNewBaselineDatasetMapper] Full TransformGens used in training: {}".format(
                str(self.tfm_gens)
            )
        )

        self.img_format = image_format
        self.is_train = is_train
        self.meta = meta
        self.num_queries = num_queries

        self.things = []
        for k,v in self.meta.thing_dataset_id_to_contiguous_id.items():
            self.things.append(v)
        self.class_names = self.meta.thing_classes
        self.text_tokenizer = Tokenize(SimpleTokenizer(), max_seq_len=max_seq_len)
        self.task_tokenizer = Tokenize(SimpleTokenizer(), max_seq_len=task_seq_len)
        self.use_big_copy_paste = True
        if self.use_big_copy_paste:
            meta_aug = MetadataCatalog.get("hanwha_train_aug")
            paste_dataset = COCO(meta_aug.json_file)
            paste_img_root = meta_aug.image_root
            self.bigcopypaste = BigCopyPasteAugmentation(
                paste_dataset, paste_img_root)

    @classmethod
    def from_config(cls, cfg, is_train=True):
        # Build augmentation
        tfm_gens = build_transform_gen(cfg, is_train)
        dataset_names = cfg.DATASETS.TRAIN
        meta = MetadataCatalog.get(dataset_names[0])

        ret = {
            "is_train": is_train,
            "meta": meta,
            "tfm_gens": tfm_gens,
            "image_format": cfg.INPUT.FORMAT,
            "num_queries": cfg.MODEL.ONE_FORMER.NUM_OBJECT_QUERIES - cfg.MODEL.TEXT_ENCODER.N_CTX,
            "task_seq_len": cfg.INPUT.TASK_SEQ_LEN,
            "max_seq_len": cfg.INPUT.MAX_SEQ_LEN,
        }
        return ret
    
    def _get_texts(self, classes, num_class_obj):
        
        classes = list(np.array(classes))
        texts = ["an instance photo"] * self.num_queries
        
        for class_id in classes:
            cls_name = self.class_names[class_id]
            num_class_obj[cls_name] += 1
        
        num = 0
        for i, cls_name in enumerate(self.class_names):
            if num_class_obj[cls_name] > 0:
                for _ in range(num_class_obj[cls_name]):
                    if num >= len(texts):
                        break
                    texts[num] = f"a photo with a {cls_name}"
                    num += 1

        return texts
    
    def __call__(self, dataset_dict):
        """
        Args:
            dataset_dict (dict): Metadata of one image, in Detectron2 Dataset format.

        Returns:
            dict: a format that builtin models in detectron2 accept
        """
        dataset_dict = copy.deepcopy(dataset_dict)  # it will be modified by code below
        image = utils.read_image(dataset_dict["file_name"], format=self.img_format)
        utils.check_image_size(dataset_dict, image)
        annos = dataset_dict.get("annotations", [])

        if self.use_big_copy_paste and len(annos) > 0:
            masks = [convert_coco_poly_to_mask_(anno["segmentation"], image.shape[0], image.shape[1]) for anno in annos]
            boxes = [anno["bbox"] for anno in annos]
            labels = [anno["category_id"] for anno in annos]
            label_dict = {"labels": np.array(labels)}
        # # TODO: get padding mask
        # # by feeding a "segmentation mask" to the same transforms
        padding_mask = np.ones(image.shape[:2])

        image_, masks, boxes, label_dict = self.bigcopypaste(image, masks, boxes, label_dict )
        image, transforms = T.apply_transform_gens(self.tfm_gens, image_)
        new_annotations = []
        masks = binary_masks_to_polygons(masks)
        for i in range(len(masks)):
            x1, y1, x2, y2 = boxes[i]
            w = x2 - x1
            h = y2 - y1
            
            new_annotations.append({
                "iscrowd": 0,
                "bbox": [float(x1), float(y1), float(w), float(h)],
                "category_id": int(label_dict["labels"][i]),
                "segmentation": masks[i],
                "bbox_mode": BoxMode.XYWH_ABS,
            })

        dataset_dict["annotations"] = new_annotations
        # the crop transformation has default padding value 0 for segmentation
        padding_mask = transforms.apply_segmentation(padding_mask)
        padding_mask = ~ padding_mask.astype(bool)

        image_shape = image.shape[:2]  # h, w

        # Pytorch's dataloader is efficient on torch.Tensor due to shared-memory,
        # but not efficient on large generic data structures due to the use of pickle & mp.Queue.
        # Therefore it's important to use torch.Tensor.
        dataset_dict["image"] = torch.as_tensor(np.ascontiguousarray(image.transpose(2, 0, 1)))
        dataset_dict["padding_mask"] = torch.as_tensor(np.ascontiguousarray(padding_mask))

        if not self.is_train:
            # USER: Modify this if you want to keep them for some reason.
            dataset_dict.pop("annotations", None)
            return dataset_dict
        # augmentation 결과 instances에 추가하기 
        # if len(masks) > 0:
        #     masks_tensor = BitMasks(torch.stack([torch.from_numpy(m).squeeze(0) for m in masks]))
        #     masks_tensor = transforms.apply_segmentation(masks_tensor)

        #     boxes_tensor = Boxes(torch.tensor(boxes, dtype=torch.float32))
        #     boxes_tensor = transforms.apply_box(boxes_tensor.tensor)
        #     classes_tensor = torch.tensor(label_dict["labels"], dtype=torch.int64)

        #     instances = Instances(image_shape)
        #     instances.gt_masks = masks_tensor.tensor
        #     instances.gt_boxes = boxes_tensor
        #     instances.gt_classes = classes_tensor
        #     instances = utils.filter_empty_instances(instances)
        # else:
        #     instances = utils.annotations_to_instances(annos, image_shape)

        if "annotations" in dataset_dict:
            # USER: Modify this if you want to keep them for some reason.
            for anno in dataset_dict["annotations"]:
                anno.pop("keypoints", None)

            # USER: Implement additional transformations if you have other types of data
            annos = [
                utils.transform_instance_annotations(obj, transforms, image_shape)
                for obj in dataset_dict.pop("annotations")
                if obj.get("iscrowd", 0) == 0
            ]

            instances = utils.annotations_to_instances(annos, image_shape)
        
            instances.gt_boxes = instances.gt_masks.get_bounding_boxes()
            # Need to filter empty instances first (due to augmentation)
            instances = utils.filter_empty_instances(instances)
            # Generate masks from polygon
            h, w = instances.image_size
            # image_size_xyxy = torch.as_tensor([w, h, w, h], dtype=torch.float)
            if hasattr(instances, 'gt_masks'):
                gt_masks = instances.gt_masks
                gt_masks = convert_coco_poly_to_mask(gt_masks.polygons, h, w)
                instances.gt_masks = gt_masks
        

        num_class_obj = {}
        for name in self.class_names:
            num_class_obj[name] = 0

        task = "The task is instance"
        text = self._get_texts(instances.gt_classes, num_class_obj)

        dataset_dict["instances"] = instances
        dataset_dict["orig_shape"] = image_shape
        dataset_dict["task"] = task
        dataset_dict["text"] = text
        dataset_dict["thing_ids"] = self.things

        return dataset_dict
