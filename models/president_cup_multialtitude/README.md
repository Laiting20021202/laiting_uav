# Multi-altitude vehicle / tank detector

`best.pt` is the deployment checkpoint. It is a YOLO11m detector with class 0
`vehicle` and class 1 `tank`, using `imgsz=1280`.

Training starts from the preliminary-round VisDrone YOLO11m checkpoint. The
training view combines altitude-balanced simulator captures at 50, 75, 100,
150, 225, 325, and 450 m with 180 images from `tank_dataset/train`. Twenty
additional target-training images are used during model selection.
`tank_dataset/val` is excluded from both training and model selection and is
used only for final acceptance.

Key reports:

- `evaluation/simulation_test_metrics.json`: balanced held-out simulator test.
- `evaluation/per_altitude_metrics.json`: new held-out simulator episodes split by height.
- `evaluation/tank_dataset_metrics.json`: final target-domain acceptance.
- `evaluation/tank_dataset_bbox/`: all 50 target-domain images with detections.

The normal simulator now selects this checkpoint by default. Restart the
simulator/monitor after changing the YAML so the running process reloads it.
