import time
import argparse
import numpy as np
from typing import Dict, Optional
import ipdb
from pathlib import Path
from tqdm import tqdm
import os

import torch
import lietorch
import droid_backends
from lietorch import SE3
import cv2
import open3d as o3d

from matplotlib.pyplot import get_cmap
import matplotlib.pyplot as plt
import matplotlib as mpl

# Ignore warnings [DANGEROUS] (activate this when debugging!)
# o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Error)
from .geom import projective_ops as pops

CAM_POINTS = np.array(
    [
        [0, 0, 0],
        [-1, -1, 1.5],
        [1, -1, 1.5],
        [1, 1, 1.5],
        [-1, 1, 1.5],
        [-0.5, 1, 1.5],
        [0.5, 1, 1.5],
        [0, 1.2, 1.5],
    ]
)

CAM_LINES = np.array(
    [[1, 2], [2, 3], [3, 4], [4, 1], [1, 0], [0, 2], [3, 0], [0, 4], [5, 7], [7, 6]]
)


def depth2rgb(depths: torch.Tensor) -> np.ndarray:
    """Convert a depth array to a color representation.

    args:
    ---
    depths [torch.Tensor]: Depth tensor of shape (N, H, W) or (H, W)
    """
    if depths.ndim == 2:
        depths = depths.unsqueeze(0)

    depths = depths.cpu().numpy()
    rgb = []
    for depth in depths:
        rgb.append(get_clipped_depth_visualization(depth))
    return np.asarray(rgb)[..., :3]


def get_clipped_depth_visualization(
    depth: np.ndarray,
    min_depth: float = 0.0,
    max_depth: float = 5.0,
    cmap: str = "Spectral",
) -> np.ndarray:
    """
    Get a color image of a depth/disparity array. This normalizes the values
    and cuts off extreme outliers, so that we get a good picture of the scene geometry.
    Color map can be choosen to be any matplotlib colormap, common choices for depth are
    "magma", "gray", "spectral", "plasma" or "turbo".
    """
    vinds = depth > 0
    depth_rgb = array2rgb(depth, cmap=cmap, vmin=min_depth, vmax=max_depth)
    # Just mark invalid pixels black
    depth_rgb[~vinds] = np.array([0.0, 0.0, 0.0])
    return depth_rgb


def get_normalized_depth_visualization(
    depth: np.ndarray,
    pc: int = 98,
    crop_percent: float = 0,
    cmap: str = "magma",
    eps: float = 1e-8,
) -> np.ndarray:
    """
    Get a color image of a depth/disparity array. This normalizes the values
    and cuts off extreme outliers, so that we get a good picture of the scene geometry.
    Color map can be choosen to be any matplotlib colormap, common choices for depth are
    "magma", "gray" or "turbo".

    NOTE If the depth map is a constant 0.0 aka your model produced garbage, this simply
    returns the input array.
    """

    vinds = depth > 0
    # convert to disparity
    depth = 1.0 / (depth + 1)

    z1 = np.percentile(depth[vinds], pc)
    z2 = np.percentile(depth[vinds], 100 - pc)

    depth = (depth - z2) / ((z1 - z2) + eps)
    depth = np.clip(depth, 0, 1)

    depth_rgb = array2rgb(depth, cmap=cmap)
    # NOTE when we use this function for smoothness maps, we sometimes have an all False array
    if np.all(vinds):
        keep_H = int(depth_rgb.shape[0] * (1 - crop_percent))
    else:
        # Just mark invalid pixels black
        depth_rgb[~vinds] = np.array([0.0, 0.0, 0.0])
        keep_H = int(depth_rgb.shape[0] * (1 - crop_percent))
    return depth_rgb[:keep_H]


def array2rgb(
    im: np.ndarray,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    cmap: str = "gray",
) -> np.ndarray:
    """
    Convert array to color map, if given limits [vmin, vmax], the values are normalized.

    args:
    ---
    im: Numpy array of shape [H x W], [H x W x 1] or [B x H x W x 1]

    returns:
    ---
    rgb_img: RGB array of shape [H x W x 3] or [B x H x W x 3]
    """
    cmap = get_cmap(cmap)
    norm = mpl.colors.Normalize(vmin=vmin, vmax=vmax)
    rgba_img = cmap(norm(im).astype(np.float32))
    rgb_img = np.delete(rgba_img, 3, -1)
    return rgb_img


def white_balance(img):
    # from https://stackoverflow.com/questions/46390779/automatic-white-balancing-with-grayworld-assumption
    result = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    avg_a = np.average(result[:, :, 1])
    avg_b = np.average(result[:, :, 2])
    result[:, :, 1] = result[:, :, 1] - (
        (avg_a - 128) * (result[:, :, 0] / 255.0) * 1.1
    )
    result[:, :, 2] = result[:, :, 2] - (
        (avg_b - 128) * (result[:, :, 0] / 255.0) * 1.1
    )
    result = cv2.cvtColor(result, cv2.COLOR_LAB2BGR)
    return result


def create_camera_actor(g, scale=0.05):
    """build open3d camera polydata"""
    camera_actor = o3d.geometry.LineSet(
        points=o3d.utility.Vector3dVector(scale * CAM_POINTS),
        lines=o3d.utility.Vector2iVector(CAM_LINES),
    )

    color = (g * 1.0, 0.5 * (1 - g), 0.9 * (1 - g))
    camera_actor.paint_uniform_color(color)
    return camera_actor


def create_point_actor(points, colors):
    """open3d point cloud from numpy array"""
    point_cloud = o3d.geometry.PointCloud()
    point_cloud.points = o3d.utility.Vector3dVector(points)
    point_cloud.colors = o3d.utility.Vector3dVector(colors)
    return point_cloud


def set_load_view_point(filename):
    ctr = vis.get_view_control()
    param = o3d.io.read_pinhole_camera_parameters(filename)
    ctr.convert_from_pinhole_camera_parameters(param)


def write_pointclouds(pointclouds: Dict, target_folder: str, ext: str = "ply") -> None:
    """Write out a list of point cloud geometries, so we can reload these in another script."""
    if not os.path.exists(os.path.join(os.getcwd(), target_folder)):
        os.makedirs(os.path.join(os.getcwd(), target_folder))

    for key, pointcloud in tqdm(pointclouds.items()):
        o3d.io.write_point_cloud(
            os.path.join(os.getcwd(), target_folder, str(key).zfill(4) + "." + ext),
            pointcloud,
        )
    return


def write_linesets(linesets: Dict, target_folder: str, ext: str = "ply") -> None:
    """Write out a list of camera actor linesets, so we can reload these in another script."""
    if not os.path.exists(os.path.join(os.getcwd(), target_folder)):
        os.makedirs(os.path.join(os.getcwd(), target_folder))

    for key, lineset in tqdm(linesets.items()):
        o3d.io.write_line_set(
            os.path.join(os.getcwd(), target_folder, str(key).zfill(4) + "." + ext),
            lineset,
        )
    return


def droid_visualization(video, save_root: str = "results", device="cuda:0"):
    """DROID visualization frontend"""

    torch.cuda.set_device(device)
    droid_visualization.video = video
    droid_visualization.cameras = {}
    droid_visualization.points = {}
    droid_visualization.warmup = 8
    droid_visualization.scale = 10.0  # 1.0
    droid_visualization.camera_scale = 0.05
    droid_visualization.ix = 0

    droid_visualization.filter_thresh = 0.005
    droid_visualization.do_reset = True

    def increase_filter(vis):
        droid_visualization.filter_thresh *= 2
        with droid_visualization.video.get_lock():
            droid_visualization.video.dirty[
                : droid_visualization.video.counter.value
            ] = True

    def decrease_filter(vis):
        droid_visualization.filter_thresh *= 0.5
        with droid_visualization.video.get_lock():
            droid_visualization.video.dirty[
                : droid_visualization.video.counter.value
            ] = True

    def deactivate_update(vis):
        with droid_visualization.video.get_lock():
            droid_visualization.video.dirty[:] = False

    def increase_camera(vis):
        droid_visualization.camera_scale *= 1 / 0.8
        with droid_visualization.video.get_lock():
            droid_visualization.video.dirty[
                : droid_visualization.video.counter.value
            ] = True

    def decrease_camera(vis):
        droid_visualization.camera_scale *= 0.8
        with droid_visualization.video.get_lock():
            droid_visualization.video.dirty[
                : droid_visualization.video.counter.value
            ] = True

    def start_stop_view_resetting(vis):
        droid_visualization.do_reset = not droid_visualization.do_reset

    def animation_callback(vis):
        cam_params = vis.get_view_control().convert_to_pinhole_camera_parameters()

        with torch.no_grad():

            with video.get_lock():
                t = video.counter.value
                (dirty_index,) = torch.where(video.dirty.clone())
                dirty_index = dirty_index

            if len(dirty_index) == 0:
                return

            video.dirty[dirty_index] = False

            # convert poses to 4x4 matrix
            poses = torch.index_select(video.poses, 0, dirty_index)
            disps = torch.index_select(video.disps, 0, dirty_index)
            Ps = SE3(poses).inv().matrix().cpu().numpy()

            images = torch.index_select(video.images, 0, dirty_index)
            # images = images.cpu()[:, [2, 1, 0], 3::8, 3::8].permute(0, 2, 3, 1)
            images = images.cpu()[:, ..., 3::8, 3::8].permute(0, 2, 3, 1)
            points = droid_backends.iproj(
                SE3(poses).inv().data, disps, video.intrinsics[0]
            ).cpu()

            thresh = droid_visualization.filter_thresh * torch.ones_like(
                disps.mean(dim=[1, 2])
            )

            count = droid_backends.depth_filter(
                video.poses, video.disps, video.intrinsics[0], dirty_index, thresh
            )

            count, disps = count.cpu(), disps.cpu()
            masks = (count >= 2) & (disps > 0.5 * disps.mean(dim=[1, 2], keepdim=True))

            for i in range(len(dirty_index)):
                pose = Ps[i]
                ix = dirty_index[i].item()

                if ix in droid_visualization.cameras:
                    vis.remove_geometry(
                        droid_visualization.cameras[ix],
                        reset_bounding_box=droid_visualization.do_reset,
                    )
                    del droid_visualization.cameras[ix]

                if ix in droid_visualization.points:
                    vis.remove_geometry(
                        droid_visualization.points[ix],
                        reset_bounding_box=droid_visualization.do_reset,
                    )
                    del droid_visualization.points[ix]

                ### add camera actor ###
                cam_actor = create_camera_actor(True, droid_visualization.camera_scale)
                cam_actor.transform(pose)
                vis.add_geometry(
                    cam_actor, reset_bounding_box=droid_visualization.do_reset
                )
                droid_visualization.cameras[ix] = cam_actor

                mask = masks[i].reshape(-1)
                pts = points[i].reshape(-1, 3)[mask].cpu().numpy()
                clr = images[i].reshape(-1, 3)[mask].cpu().numpy()

                ## add point actor ###
                point_actor = create_point_actor(pts, clr)
                vis.add_geometry(
                    point_actor, reset_bounding_box=droid_visualization.do_reset
                )
                droid_visualization.points[ix] = point_actor

            # hack to allow interacting with vizualization during inference
            # if len(droid_visualization.cameras) >= droid_visualization.warmup:
            #     cam = vis.get_view_control().convert_from_pinhole_camera_parameters(cam)

            droid_visualization.ix += 1

            cam = vis.get_view_control().convert_from_pinhole_camera_parameters(
                cam_params, True
            )
            vis.poll_events()
            vis.update_renderer()

    ### create Open3D visualization ###
    vis = o3d.visualization.VisualizerWithKeyCallback()
    vis.register_animation_callback(animation_callback)
    vis.register_key_callback(ord("S"), increase_filter)
    vis.register_key_callback(ord("A"), decrease_filter)
    vis.register_key_callback(ord("Q"), deactivate_update)
    vis.register_key_callback(ord("M"), increase_camera)
    vis.register_key_callback(ord("N"), decrease_camera)
    vis.register_key_callback(ord("R"), start_stop_view_resetting)

    vis.create_window(height=540, width=960)
    vis.get_render_option().load_from_json("src/renderoption.json")

    vis.run()  # user changes the view and press "q" to terminate
    param = vis.get_view_control().convert_to_pinhole_camera_parameters()

    ### Store the geometry and trajectory visualizaton for later inspection when done
    print("Saving the visualization for later usage ...")
    try:
        o3d.io.write_pinhole_camera_parameters("final_viewpoint.json", param)
        pcl_path = str(Path(save_root) / "pointclouds")
        cam_path = str(Path(save_root) / "cameras")
        write_pointclouds(droid_visualization.points, pcl_path, ext="xyzrgb")
        write_linesets(droid_visualization.cameras, cam_path)
    except Exception as e:
        print("Something went wrong when saving the visualization")
        print(e)

    vis.destroy_window()
