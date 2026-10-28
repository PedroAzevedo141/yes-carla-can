"""Explicit YOLOv5 postprocessing with CUDA-aware wall-clock stage timings."""
import time

import torch
from torchvision.ops import box_iou, nms

from .backend import sync


def postprocess(prediction, conf=0.25, iou=0.45, max_nms=30000, max_det=30000, multi_label=True):
    """Single image; equivalent to author NMS without its batch timeout.

    The author artifact defaults to multi_label=True. Record proposal count
    separately from expanded class rows, which can be larger.
    """
    sync(prediction.device)
    start = time.perf_counter()
    x = prediction[prediction[:, 4] > conf]
    scores = x[:, 5:] * x[:, 4:5]
    qualified = int((scores.max(1).values > conf).sum()) if len(x) else 0
    box = x[:, :4].clone()
    box[:, :2] = x[:, :2] - x[:, 2:4] / 2
    box[:, 2:4] = x[:, :2] + x[:, 2:4] / 2
    if multi_label:
        row, cls = (scores > conf).nonzero(as_tuple=True)
        detections = torch.cat((box[row], scores[row, cls, None], cls[:, None].float()), 1)
    elif len(x):
        score, cls = scores.max(1)
        detections = torch.cat((box, score[:, None], cls[:, None].float()), 1)[score > conf]
    else:
        detections = x.new_empty((0, 6))
    expanded = len(detections)
    if expanded > max_nms:
        detections = detections[detections[:, 4].argsort(descending=True)[:max_nms]]
    boxes = detections[:, :4] + detections[:, 5:6] * 4096
    sync(prediction.device)
    before_nms = time.perf_counter()
    keep = nms(boxes, detections[:, 4], iou)[:max_det]
    result = detections[keep]
    sync(prediction.device)
    end = time.perf_counter()
    return result, {
        "qualified_proposals": qualified,
        "expanded_candidates": expanded,
        "nms_input": len(detections),
        "nms_ms": (end - before_nms) * 1000,
        "postprocess_ms": (end - start) * 1000,
    }


def preservation(clean, attacked, iou_threshold=0.45):
    """Same-class maximum-IoU matching, as in the artifact; no ground truth.

    No clean detections is undefined (None), rather than artificial 100%.
    This matches the paper's preservation metric, not one-to-one GT recall.
    """
    if not len(clean):
        return None, 0, 0
    matched = 0
    for cls in clean[:, 5].unique():
        a = clean[clean[:, 5] == cls, :4]
        b = attacked[attacked[:, 5] == cls, :4]
        if len(b):
            matched += int((box_iou(a, b).max(1).values > iou_threshold).sum())
    return matched / len(clean), matched, len(clean)
