"""Keep class scores for the same boxes selected by the live YOLO predictor."""
from ultralytics.models.yolo.detect import DetectionPredictor
from ultralytics.utils import nms, ops


class CandidatePredictor(DetectionPredictor):
    def postprocess(self, preds, img, orig_imgs, **kwargs):
        raw = preds[0] if isinstance(preds, (list, tuple)) else preds
        # Some exported models already contain NMS and only provide the winning
        # class. Preserve their normal output instead of inventing other scores.
        if getattr(self.model, "end2end", False) or raw.shape[-1] == 6:
            return super().postprocess(preds, img, orig_imgs, **kwargs)

        scores = raw[:, 4:4 + len(self.model.names), :]
        boxes, indices = nms.non_max_suppression(
            preds, self.args.conf, self.args.iou, self.args.classes,
            self.args.agnostic_nms, max_det=self.args.max_det, return_idxs=True,
        )
        if not isinstance(orig_imgs, list):
            orig_imgs = ops.convert_torch2numpy_batch(orig_imgs)[..., ::-1]
        results = self.construct_results(boxes, img, orig_imgs, **kwargs)
        for result, image_scores, selected in zip(results, scores, indices):
            # NMS indices align every score vector with its surviving box, even
            # after confidence sorting, suppression and class filtering.
            result.class_scores = image_scores[:, selected.long().reshape(-1)].T.float().cpu().tolist()
        return results
