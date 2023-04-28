# Copyright (c) OpenMMLab. All rights reserved.
import torch

from mmrotate.core import rbbox2roi
from ..builder import ROTATED_HEADS
from .rotate_standard_roi_head import RotatedStandardRoIHead
from mmrotate.core import build_assigner, build_sampler, obb2xyxy, rbbox2result


@ROTATED_HEADS.register_module()
class ProOrientedStandardRoIHead(RotatedStandardRoIHead):
    """Oriented RCNN roi head including one bbox head."""

    def forward_dummy(self, x, proposals):
        """Dummy forward function.

        Args:
            x (list[Tensors]): list of multi-level img features.
            proposals (list[Tensors]): list of region proposals.

        Returns:
            list[Tensors]: list of region of interest.
        """
        outs = ()
        rois = rbbox2roi([proposals])
        if self.with_bbox:
            bbox_results = self._bbox_forward(x, rois)
            outs = outs + (bbox_results['cls_score'],
                           bbox_results['bbox_pred'])
        return outs

    def forward_train(self,
                      x,
                      img_metas,
                      proposal_list,
                      gt_bboxes,
                      gt_labels,
                      bbox_results,
                      gt_bboxes_ignore=None,
                      gt_masks=None):

        # assign gts and sample proposals
        if self.with_bbox:

            num_imgs = len(img_metas)
            if gt_bboxes_ignore is None:
                gt_bboxes_ignore = [None for _ in range(num_imgs)]
            sampling_results = []
            for i in range(num_imgs):
                assign_result = self.bbox_assigner.assign(
                    proposal_list[i], gt_bboxes[i], gt_bboxes_ignore[i],
                    gt_labels[i])
                sampling_result = self.bbox_sampler.sample(
                    assign_result,
                    proposal_list[i],
                    gt_bboxes[i],
                    gt_labels[i],
                    feats=[lvl_feat[i][None] for lvl_feat in x])

                if gt_bboxes[i].numel() == 0:
                    sampling_result.pos_gt_bboxes = gt_bboxes[i].new(
                        (0, gt_bboxes[0].size(-1))).zero_()
                else:
                    sampling_result.pos_gt_bboxes = \
                        gt_bboxes[i][sampling_result.pos_assigned_gt_inds, :]

                sampling_results.append(sampling_result)

        losses = dict()
        # bbox head forward and loss
        if self.with_bbox:
            bbox_results = self._bbox_forward_train(x, bbox_results, sampling_results,
                                                    gt_bboxes, gt_labels,
                                                    img_metas)
            losses.update(bbox_results['loss_cls_fine'])

        return losses

    def _bbox_forward1(self, x, bbox_results):
        # bbox_feats = self.bbox_roi_extractor(
        #     x[:self.bbox_roi_extractor.num_inputs], rois)
        bbox_feats = bbox_results['bbox_feats']
        if self.with_shared_head:
            bbox_feats = self.shared_head(bbox_feats)
        cls_score = self.bbox_head(bbox_feats, bbox_results['cls_score'])

        bbox_results = dict(
            cls_score=cls_score)
        return bbox_results

    def _bbox_forward_train(self, x, bbox_results, sampling_results, gt_bboxes, gt_labels,
                            img_metas):

        rois = rbbox2roi([res.bboxes for res in sampling_results])
        bbox_results = self._bbox_forward1(x, bbox_results)

        bbox_targets = self.bbox_head.get_targets(sampling_results, gt_bboxes,
                                                  gt_labels, self.train_cfg)
        loss_bbox = self.bbox_head.loss(bbox_results['cls_score'],
                                        None, rois,
                                        *bbox_targets)

        loss_cls = {"loss_cls_fine": loss_bbox.pop("loss_cls")}

        bbox_results.update(loss_cls_fine=loss_cls)
        return bbox_results

    def simple_test_bboxes(self,
                           x,
                           img_metas,
                           proposals,
                           bbox_results,
                           coarse_results,
                           rcnn_test_cfg,
                           rescale=False):

        rois = rbbox2roi(proposals)
        bbox_results = self._bbox_forward1(x, coarse_results)
        img_shapes = tuple(meta['img_shape'] for meta in img_metas)
        scale_factors = tuple(meta['scale_factor'] for meta in img_metas)

        # split batch bbox prediction back to each image
        cls_score = bbox_results['cls_score']
        bbox_pred = coarse_results['bbox_pred']

        num_proposals_per_img = tuple(len(p) for p in proposals)
        rois = rois.split(num_proposals_per_img, 0)
        cls_score = cls_score.split(num_proposals_per_img, 0)

        # some detector with_reg is False, bbox_pred will be None
        if bbox_pred is not None:
            # the bbox prediction of some detectors like SABL is not Tensor
            if isinstance(bbox_pred, torch.Tensor):
                bbox_pred = bbox_pred.split(num_proposals_per_img, 0)
            else:
                bbox_pred = self.bbox_head.bbox_pred_split(
                    bbox_pred, num_proposals_per_img)
        else:
            bbox_pred = (None,) * len(proposals)

        # apply bbox post-processing to each image individually
        det_bboxes = []
        det_labels = []
        for i in range(len(proposals)):
            det_bbox, det_label = self.bbox_head.get_bboxes(
                rois[i],
                cls_score[i],
                bbox_pred[i],
                img_shapes[i],
                scale_factors[i],
                rescale=rescale,
                cfg=rcnn_test_cfg)
            det_bboxes.append(det_bbox)
            det_labels.append(det_label)
        return det_bboxes, det_labels

    def simple_test(self, x, proposal_list, img_metas, bbox_results, coarse_results, rescale=False):
        assert self.with_bbox, 'Bbox head must be implemented.'

        det_bboxes, det_labels = self.simple_test_bboxes(
            x, img_metas, proposal_list, bbox_results, coarse_results, self.test_cfg, rescale=rescale)

        bbox_results = [
            rbbox2result(det_bboxes[i], det_labels[i],
                         self.bbox_head.num_classes)
            for i in range(len(det_bboxes))
        ]

        return bbox_results