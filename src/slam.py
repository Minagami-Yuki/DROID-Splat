import os
import ipdb
import gc
from time import sleep
from typing import List, Optional
from tqdm import tqdm
from copy import deepcopy
from termcolor import colored
from collections import OrderedDict
from omegaconf import DictConfig

import cv2
import numpy as np
import pandas as pd

import torch
import torch.multiprocessing as mp
from lietorch import SE3

from .droid_net import DroidNet
from .frontend import FrontendWrapper
from .backend import BackendWrapper
from .depth_video import DepthVideo
from .geom import matrix_to_lie
from .visualization import droid_visualization, depth2rgb, uncertainty2rgb
from .trajectory_filler import PoseTrajectoryFiller
from .gaussian_mapping import GaussianMapper

from .gaussian_splatting.camera_utils import Camera
from .gaussian_splatting.eval_utils import (
    eval_rendering,
    do_odometry_evaluation,
    EvaluatePacket,
    get_gt_c2w_from_stream,
    torch_intersect1d,
)
from .gaussian_splatting.utils.graphics_utils import getProjectionMatrix2, focal2fov
from .gaussian_splatting.gui import gui_utils, slam_gui
from .utils import clone_obj


class SLAM:
    """SLAM system which bundles together multiple building blocks:
        - Frontend Tracker based on a Motion filter, which successively inserts new frames into a map
            within a local optimization window
        - Backend Bundle Adjustment, which optimizes the map over a global optimization window
        - Gaussian Mapping, which optimizes the map into multiple 3D Gaussian
            based on a dense rendering objective
        - Visualizers for showing the incoming RGB(D) stream, the current pose graph,
            the 3D point clouds of the map, optimized Gaussians

    We combine these building blocks in a multiprocessing environment.
    """

    def __init__(self, cfg, dataset=None, output_folder: Optional[str] = None):
        super(SLAM, self).__init__()

        self.cfg = cfg
        # FIXME chen: include sanity checks to see if the system is correctly configured
        # example: it is dangerous to optimize the intrinsics and do scale optimization with our block coordinate descent scheme

        self.device = cfg.get("device", torch.device("cuda:0"))
        self.mode = cfg.mode

        # evaluation params
        self.do_evaluate = cfg.evaluate
        # Render every 5-th frame during optimization, so we can see the development of our scene representation
        self.save_renders = cfg.get("save_renders", False)

        self.create_out_dirs(output_folder)
        self.update_cam(cfg)

        self.net = DroidNet()
        self.load_pretrained(cfg.tracking.pretrained)
        self.net.to(self.device).eval()
        self.net.share_memory()

        # Manage life time of individual processes
        self.num_running_thread = torch.zeros((1)).int()
        self.num_running_thread.share_memory_()
        self.all_trigered = torch.zeros((1)).int()
        self.all_trigered.share_memory_()
        self.all_finished = torch.zeros((1)).int()
        self.all_finished.share_memory_()
        self.tracking_finished = torch.zeros((1)).int()
        self.tracking_finished.share_memory_()
        self.gaussian_mapping_finished = torch.zeros((1)).int()
        self.gaussian_mapping_finished.share_memory_()
        self.backend_finished = torch.zeros((1)).int()
        self.backend_finished.share_memory_()
        self.visualizing_finished = torch.zeros((1)).int()
        self.visualizing_finished.share_memory_()

        self.mapping_visualizing_finished = torch.zeros((1)).int()
        self.mapping_visualizing_finished.share_memory_()

        # Insert a dummy delay to snychronize frontend and backend as needed
        self.sleep_time = cfg.get("sleep_delay", 3)
        self.t_start = cfg.get("t_start", 0)
        self.t_stop = cfg.get("t_stop", None)

        # Delete backend when hitting this threshold, so we can keep going with just frontend
        self.max_ram_usage = cfg.get("max_ram_usage", 0.9)
        self.plot_uncertainty = cfg.get("plot_uncertainty", False)  # Show the optimization uncertainty maps

        # Stream the images into the main thread
        self.input_pipe = mp.Queue()

        # store images, depth, poses, intrinsics (shared between process)
        self.video = DepthVideo(cfg)  # NOTE: we can use this for getting both gt and rendered images
        self.frontend = FrontendWrapper(cfg, self)
        self.backend = BackendWrapper(cfg, self)
        self.traj_filler = PoseTrajectoryFiller(self.cfg, net=self.net, video=self.video, device=self.device)

        self.dataset = dataset
        self.mapping_queue = mp.Queue()
        self.received_mapping = mp.Event()

        if cfg.data.dataset in ["kitti", "tartanair", "euroc"]:
            self.max_depth_visu = 50.0  # Cut of value to show a consistent depth stream in outdoor datasets
        else:
            self.max_depth_visu = 10.0  # Good value for indoor datasets (maybe make this even lower)

        if cfg.run_mapping_gui and cfg.run_mapping and not cfg.evaluate:
            self.q_main2vis = mp.Queue()
            self.gaussian_mapper = GaussianMapper(cfg, self, gui_qs=(self.q_main2vis))
            self.params_gui = gui_utils.ParamsGUI(
                pipe=cfg.mapping.pipeline_params,
                background=self.gaussian_mapper.background,
                gaussians=self.gaussian_mapper.gaussians,
                q_main2vis=self.q_main2vis,
            )
        else:
            self.gaussian_mapper = GaussianMapper(cfg, self)

        self.sanity_checks()

    def info(self, msg) -> None:
        print(colored("[Main]: " + msg, "green"))

    # TODO chen: what are other corner cases which we would like a user to avoid?
    def sanity_checks(self):
        """Perform sanity checks to see if the system is misconfigured, this is just supposed
        to protect the user when running the system"""
        if self.cfg.mode == "stereo":
            # NOTE chen: I noticed, that this is really not impemented, i.e.
            # we would need to do some changes in motion_filter, depth_video, BA, etc. to even store the right images, fmaps, etc.
            raise NotImplementedError(colored("Stereo mode not supported yet!", "red"))
        if self.cfg.mode == "prgbd":
            assert not (self.video.optimize_scales and self.video.opt_intr), colored(
                """Optimizing both poses, disparities, scale & shift and 
            intrinsics creates unforeseen ambiguities!
            This is usually not stable :(
            """,
                "red",
            )
        if self.cfg.run_mapping:
            if self.cfg.mapping.use_non_keyframes:
                assert self.cfg.mapping.refinement_iters > 0, colored(
                    """If you want to use non-keyframes during Gaussian Rendering Optimization, 
                    make sure that you actually refine the map after running tracking!""",
                    "red",
                )

    def create_out_dirs(self, output_folder: Optional[str] = None) -> None:
        if output_folder is not None:
            self.output = output_folder
        else:
            self.output = "./outputs/"

        os.makedirs(self.output, exist_ok=True)
        if self.save_renders:
            os.makedirs(f"{self.output}/intermediate_renders/", exist_ok=True)
            os.makedirs(f"{self.output}/intermediate_renders/final", exist_ok=True)
            os.makedirs(f"{self.output}/intermediate_renders/temp", exist_ok=True)
        os.makedirs(f"{self.output}/evaluation", exist_ok=True)

    def update_cam(self, cfg):
        """
        Update the camera intrinsics according to the pre-processing config,
        such as resize or edge crop
        """
        # resize the input images to crop_size(variable name used in lietorch)
        H, W = float(cfg.data.cam.H), float(cfg.data.cam.W)
        fx, fy = cfg.data.cam.fx, cfg.data.cam.fy
        cx, cy = cfg.data.cam.cx, cfg.data.cam.cy

        h_edge, w_edge = cfg.data.cam.H_edge, cfg.data.cam.W_edge
        H_out, W_out = cfg.data.cam.H_out, cfg.data.cam.W_out

        self.fx = fx * (W_out + w_edge * 2) / W
        self.fy = fy * (H_out + h_edge * 2) / H
        self.cx = cx * (W_out + w_edge * 2) / W
        self.cy = cy * (H_out + h_edge * 2) / H
        self.H, self.W = H_out, W_out

        self.cx = self.cx - w_edge
        self.cy = self.cy - h_edge

    # TODO chen: do we really still need this?
    def load_bound(self, cfg: DictConfig) -> None:
        self.bound = torch.from_numpy(np.array(cfg.data.bound)).float()

    def load_pretrained(self, pretrained: str) -> None:
        self.info(f"Load pretrained checkpoint from {pretrained}!")

        # TODO why do we have to use the [:2] here?!
        state_dict = OrderedDict([(k.replace("module.", ""), v) for (k, v) in torch.load(pretrained).items()])
        state_dict["update.weight.2.weight"] = state_dict["update.weight.2.weight"][:2]
        state_dict["update.weight.2.bias"] = state_dict["update.weight.2.bias"][:2]
        state_dict["update.delta.2.weight"] = state_dict["update.delta.2.weight"][:2]
        state_dict["update.delta.2.bias"] = state_dict["update.delta.2.bias"][:2]

        self.net.load_state_dict(state_dict)

    def tracking(self, rank, stream, input_queue: mp.Queue) -> None:
        """Main driver of framework by looping over the input stream"""

        self.info("Frontend tracking thread started!")
        self.all_trigered += 1

        # Wait up for other threads to start
        while self.all_trigered < self.num_running_thread:
            pass

        for frame in tqdm(stream):

            if self.cfg.with_dyn and stream.has_dyn_masks:
                timestamp, image, depth, intrinsic, gt_pose, static_mask = frame
            else:
                timestamp, image, depth, intrinsic, gt_pose = frame
                static_mask = None
            if self.mode not in ["rgbd", "prgbd"]:
                depth = None

            # Control when to start and when to stop the SLAM system from outside
            if timestamp < self.t_start:
                continue
            if self.t_stop is not None and timestamp > self.t_stop:
                break

            if self.cfg.show_stream:
                # Transmit the incoming stream to another visualization thread
                input_queue.put(image)
                input_queue.put(depth)

            self.frontend(timestamp, image, depth, intrinsic, gt_pose, static_mask=static_mask)

        del self.frontend
        torch.cuda.empty_cache()
        gc.collect()

        self.tracking_finished += 1
        self.all_finished += 1
        self.info("Frontend Tracking done!")

    def get_ram_usage(self):
        free_mem, total_mem = torch.cuda.mem_get_info(device=self.device)
        used_mem = 1 - (free_mem / total_mem)
        return used_mem, free_mem

    def ram_safeguard_backend(self, max_ram: float = 0.9, min_ram: float = 0.5) -> None:
        """There are some scenes, where we might get into trouble with memory.
        In order to keep the system going, we simply dont use the backend until we can afford it again.
        """
        used_mem, free_mem = self.get_ram_usage()
        if used_mem > max_ram and self.backend is not None:
            print(colored(f"[Main]: Warning: Deleting Backend due to high memory usage [{used_mem} %]!", "red"))
            print(colored(f"[Main]: Warning: Warning: Got only {free_mem/ 1024 ** 3} GB left!", "red"))
            del self.backend
            self.backend = None
            gc.collect()
            torch.cuda.empty_cache()

        # NOTE chen: if we deleted the backend due to memory issues we likely have not a lot of capacity left for backend
        # only use backend again once we have some slack -> 50% free RAM (12GB in use)
        if self.backend is None and used_mem <= min_ram:
            self.info("Reinstantiating Backend ...")
            self.backend = BackendWrapper(self.cfg, self)
            self.backend.to(self.device)

    def global_ba(self, rank, run=False):
        self.info("Backend thread started!")
        self.all_trigered += 1

        while self.tracking_finished < 1 and run:

            # Only run backend if we have enough RAM for it
            self.ram_safeguard_backend(max_ram=self.max_ram_usage)
            if self.backend is not None:
                if self.backend.enable_loop:
                    self.backend(local_graph=self.frontend.optimizer.graph)
                else:
                    self.backend()
                sleep(self.sleep_time)  # Let multiprocessing cool down a little bit

        # Run one last time after tracking finished
        if run and self.backend is not None and self.backend.do_refinement:
            with self.video.get_lock():
                t_end = self.video.counter.value

            msg = "Optimize full map: [{}, {}]!".format(0, t_end)
            self.backend.info(msg)

            # Use loop closure BA for refinement if enabled
            if self.backend.enable_loop:
                _, _ = self.backend.optimizer.loop_ba(t_start=0, t_end=t_end, steps=6)
                _, _ = self.backend.optimizer.loop_ba(t_start=0, t_end=t_end, steps=6)
            else:
                _, _ = self.backend.optimizer.dense_ba(t_start=0, t_end=t_end, steps=6)
                _, _ = self.backend.optimizer.dense_ba(t_start=0, t_end=t_end, steps=6)

        del self.backend
        torch.cuda.empty_cache()
        gc.collect()

        self.backend_finished += 1
        self.all_finished += 1
        self.info("Backend done!")

    def gaussian_mapping(self, rank, run, mapping_queue: mp.Queue, received_mapping: mp.Event):
        self.info("Gaussian Mapping Triggered!")
        self.all_trigered += 1

        while (self.tracking_finished + self.backend_finished) < 2 and run:
            self.gaussian_mapper(mapping_queue, received_mapping)
            # sleep(self.sleep_time / 2)

        # Run for one last time after everything finished
        finished = False
        while not finished and run:
            finished = self.gaussian_mapper(mapping_queue, received_mapping, True)

        self.gaussian_mapping_finished += 1
        while self.mapping_visualizing_finished < 1:
            pass

        self.all_finished += 1
        self.info("Gaussian Mapping Done!")

    def visualizing(self, rank, run=True):
        self.info("Visualization thread started!")
        self.all_trigered += 1
        finished = False

        while (self.tracking_finished + self.backend_finished < 2) and run and not finished:
            finished = droid_visualization(self.video, device=self.device, save_root=self.output)

        self.visualizing_finished += 1
        self.all_finished += 1
        self.info("Visualization done!")

    def mapping_gui(self, rank, run=True):
        self.info("Mapping GUI thread started!")
        self.all_trigered += 1
        finished = False

        while (self.tracking_finished + self.backend_finished < 2) and run and not finished:
            finished = slam_gui.run(self.params_gui)

        # Wait for Gaussian Mapper to be finished so nothing new is put into the queue anymore
        while self.gaussian_mapping_finished < 1:
            pass

        # empty all the guis that are in params_gui so this will for sure get empty
        if run:  # NOTE Leon: It crashes if we dont check this
            while not self.params_gui.q_main2vis.empty():
                obj = self.params_gui.q_main2vis.get()
                a = clone_obj(obj)
                del obj

        self.mapping_visualizing_finished += 1
        self.all_finished += 1
        self.info("Mapping GUI done!")

    def show_stream(self, rank, input_queue: mp.Queue, run=True) -> None:
        self.info("OpenCV Image stream thread started!")
        self.all_trigered += 1

        while (self.tracking_finished + self.backend_finished < 2) and run:
            if not input_queue.empty():
                try:
                    rgb = input_queue.get()
                    depth = input_queue.get()

                    rgb_image = rgb[0, [2, 1, 0], ...].permute(1, 2, 0).clone().cpu()
                    cv2.imshow("RGB", rgb_image.numpy())
                    if self.mode in ["rgbd", "prgbd"] and depth is not None:
                        # Create normalized depth map with intensity plot
                        depth_image = depth2rgb(depth.clone().cpu(), max_depth=self.max_depth_visu)[0]
                        # Convert to BGR for cv2
                        cv2.imshow("depth", depth_image[..., ::-1])
                    cv2.waitKey(1)
                except Exception as e:
                    pass
                    # Uncomment if you observe something weird, this will exit once the stream is finished
                    # print(colored(e, "red"))
                    # print(colored("Continue ..", "red"))

            if self.plot_uncertainty:
                # Plot the uncertainty on top
                with self.video.get_lock():
                    t_cur = max(0, self.video.counter.value - 1)
                    if self.cfg.tracking.get("upsample", False):
                        uncertanity_cur = self.video.uncertainty_up[t_cur].clone()
                    else:
                        uncertanity_cur = self.video.uncertainty[t_cur].clone()
                uncertainty_img = uncertainty2rgb(uncertanity_cur)[0]
                cv2.imshow("Uncertainty", uncertainty_img[..., ::-1])
                cv2.waitKey(1)

        self.all_finished += 1
        self.info("Show stream Done!")

    def evaluate(self, stream, gaussian_mapper_last_state: Optional[EvaluatePacket] = None):

        eval_path = os.path.join(self.output, "evaluation")
        self.info("Saving evaluation results in {}".format(eval_path))
        self.info("#" * 20 + f" Results for {stream.input_folder} ...")

        #### ------------------- ####
        ### Trajectory evaluation ###
        #### ------------------- ####
        # If we dont optimize the scales of our prior, we should also not use scale_adjustment!
        if (self.cfg.mode == "prgbd" and self.video.optimize_scales) or self.cfg.mode == "mono":
            monocular = True
        else:
            monocular = False

        est_c2w_all_lie, est_c2w_kf_lie, gt_c2w_all_lie, gt_c2w_kf_lie, kf_tstamps, tstamps = self.get_trajectories(
            stream, gaussian_mapper_last_state
        )

        # Evo expects floats for timestamps
        kf_tstamps = [float(i) for i in kf_tstamps]
        tstamps = [float(i) for i in tstamps]
        kf_result_ate, all_result_ate = do_odometry_evaluation(
            eval_path, est_c2w_kf_lie, gt_c2w_kf_lie, est_c2w_all_lie, gt_c2w_all_lie, tstamps, kf_tstamps, monocular
        )
        self.info("(Keyframes only) ATE: {}".format(kf_result_ate))
        self.info("(All) ATE: {}".format(all_result_ate))
        # TODO Can we filter the Dataset name out of this to make it prettier?
        # TODO update loop config flag or add another one, because we will likely run a loop closure mechanism on top
        ### Store main results with attributes for ablation/comparison
        odometry_results = {
            "ate_on_keyframes_only": [True, False],
            "run_backend": [str(self.cfg.run_backend), str(self.cfg.run_backend)],
            "run_mapping": [str(self.cfg.run_mapping), str(self.cfg.run_mapping)],
            "stride": [str(self.cfg.stride), str(self.cfg.stride)],
            "loop_closure": [str(self.backend.enable_loop), str(self.backend.enable_loop)],
            "dataset": [stream.input_folder, stream.input_folder],
            "mode": [self.cfg.mode, self.cfg.mode],
            "ape": [kf_result_ate["mean"], all_result_ate["mean"]],
        }
        df = pd.DataFrame(odometry_results)
        df.to_csv(os.path.join(eval_path, "odometry", "evaluation_results.csv"), index=False)

        #### ------------------- ####
        ### Rendering  evaluation ###
        #### ------------------- ####
        if self.cfg.run_mapping:
            render_eval_path = os.path.join(eval_path, "rendering")

            gaussians = gaussian_mapper_last_state.gaussians
            render_cfg = gaussian_mapper_last_state.pipeline_params
            background = gaussian_mapper_last_state.background

            # Get the whole trajectory as Camera objects, so we can render them
            all_cams = self.get_all_cams_for_rendering(stream, est_c2w_all_lie, gaussian_mapper_last_state)
            # Renderer needs timestamps as int for indexing
            kf_tstamps, tstamps = [int(i) for i in kf_tstamps], [int(i) for i in tstamps]
            kf_cams = [all_cams[i] for i in kf_tstamps]

            ### Evalaute only keyframes, which we overfit to see how good that fit is
            save_dir = os.path.join(render_eval_path, "keyframes")
            kf_rnd_metrics = eval_rendering(
                kf_cams, kf_tstamps, gaussians, stream, render_cfg, background, save_dir, True, monocular
            )

            ### Evalaute on non-keyframes, which we have never seen during training
            # NOTE this is the proper metric, that people compare in papers
            _, _, nonkf_tstamps = torch_intersect1d(torch.tensor(kf_tstamps), torch.tensor(tstamps))
            nonkf_tstamps = [int(i) for i in nonkf_tstamps]
            nonkf_cams = [all_cams[i] for i in nonkf_tstamps]
            save_dir = os.path.join(render_eval_path, "non-keyframes")
            nonkf_rnd_metrics = eval_rendering(
                nonkf_cams, nonkf_tstamps, gaussians, stream, render_cfg, background, save_dir, True, monocular
            )

            rendering_results = {
                "run_backend": [str(self.cfg.run_backend), str(self.cfg.run_backend)],
                "run_mapping": [str(self.cfg.run_mapping), str(self.cfg.run_mapping)],
                "stride": [str(self.cfg.stride), str(self.cfg.stride)],
                "loop_closure": [str(self.backend.enable_loop), str(self.backend.enable_loop)],
                "dataset": [stream.input_folder, stream.input_folder],
                "mode": [self.cfg.mode, self.cfg.mode],
                "psnr": [kf_rnd_metrics["mean_psnr"], nonkf_rnd_metrics["mean_psnr"]],
                "ssim": [kf_rnd_metrics["mean_ssim"], nonkf_rnd_metrics["mean_ssim"]],
                "lpips": [kf_rnd_metrics["mean_lpips"], nonkf_rnd_metrics["mean_lpips"]],
                "extra_non_kf": [str(self.cfg.mapping.use_non_keyframes), str(self.cfg.mapping.use_non_keyframes)],
                "eval_on_keyframes": [True, False],
            }
            # Check if the dataset has depth images
            if len(stream.depth_paths) != 0:
                rendering_results["l1_depth"] = [kf_rnd_metrics["mean_l1"], nonkf_rnd_metrics["mean_l1"]]
            render_df = pd.DataFrame(rendering_results)
            render_df.to_csv(os.path.join(render_eval_path, "evaluation_results.csv"), index=False)

    def get_trajectories(self, stream, gaussian_mapper_last_state: Optional[EvaluatePacket] = None):
        """Get the poses both for the whole video sequence and only the keyframes for evaluation.
        Poses are in format [B, 7, 1] as lie elements.
        """
        # When using Gaussian Mapping, we might have already used the trajectory interpolation during refinement
        if self.cfg.run_mapping and self.cfg.mapping.refinement_iters > 0 and self.cfg.mapping.use_non_keyframes:
            assert (
                gaussian_mapper_last_state is not None
            ), "Missing GaussianMapper state for evaluation even though we ran Mapping!"
            pose_dict = self.gaussian_mapper.get_camera_trajectory(self.gaussian_mapper.cameras)
            kf_ids = torch.tensor(list(self.gaussian_mapper.idx_mapping.keys()))
            kf_tstamps = self.video.timestamp[: self.video.counter.value].int().cpu()
            # Sanity checks
            ipdb.set_trace()
            assert (
                (kf_ids == kf_tstamps).all().item()
            ), "Gaussian Mapper should contain the same keyframes as in DepthVideo!"
            assert len(list(pose_dict.keys())) == len(
                stream
            ), "After adding non-keyframes, Gaussian Mapper contain all frames of the whole video stream!"
            tstamps = list(pose_dict.keys())

            ordered_poses = dict(sorted(pose_dict.items()))
            est_w2c_all = torch.stack(list(ordered_poses.values()))
            est_c2w_all_lie = SE3.InitFromVec(matrix_to_lie(est_w2c_all)).inv().vec()
            est_c2w_kf_lie = est_c2w_all_lie[kf_ids]
            kf_tstamps = kf_tstamps.cpu().int().tolist()
        else:
            # NOTE chen: even if we have optimized the poses with the GaussianMapper, we would have fed them back
            kf_tstamps = self.video.timestamp[: self.video.counter.value].int().cpu().tolist()
            est_w2c_all, tstamps = self.traj_filler(stream, return_tstamps=True)
            est_c2w_all_lie = est_w2c_all.inv().vec().cpu()  # 7x1 Lie algebra
            est_c2w_kf_lie = est_c2w_all_lie[kf_tstamps]

        est_c2w_all_lie, est_c2w_kf_lie = est_c2w_all_lie.cpu().numpy(), est_c2w_kf_lie.cpu().numpy()
        gt_c2w_all_lie = get_gt_c2w_from_stream(stream).cpu().numpy()
        gt_c2w_kf_lie = gt_c2w_all_lie[kf_tstamps]
        return est_c2w_all_lie, est_c2w_kf_lie, gt_c2w_all_lie, gt_c2w_kf_lie, kf_tstamps, tstamps

    def get_all_cams_for_rendering(
        self, stream, est_c2w_all_lie, gaussian_mapper_last_state: Optional[EvaluatePacket] = None
    ):

        if self.cfg.mapping.use_non_keyframes:
            all_cams = gaussian_mapper_last_state.cameras
        else:
            all_cams = []
            intrinsics = self.video.intrinsics[0]  # We always have the right global intrinsics stored here
            if self.video.upsample:
                intrinsics = intrinsics * self.video.scale_factor

            for i, view in tqdm(enumerate(est_c2w_all_lie)):

                _, gt_image, gt_depth, _, _ = stream[i]
                # c2w -> w2c for initialization
                view = SE3.InitFromVec(torch.tensor(view).float().to(device=self.device)).inv().matrix()
                fx, fy, cx, cy = intrinsics
                height, width = gt_image.shape[-2:]
                fovx, fovy = focal2fov(fx, width), focal2fov(fy, height)
                projection_matrix = getProjectionMatrix2(
                    self.gaussian_mapper.z_near, self.gaussian_mapper.z_far, cx, cy, fx, fy, width, height
                )
                projection_matrix = projection_matrix.transpose(0, 1).to(device=self.device)
                new_cam = Camera(
                    i,
                    gt_image.contiguous(),
                    gt_depth,
                    gt_depth,
                    view,
                    projection_matrix,
                    (fx, fy, cx, cy),
                    (fovx, fovy),
                    (height, width),
                    device=self.device,
                )
                all_cams.append(new_cam)
        return all_cams

    def save_state(self):
        self.info("Saving checkpoints...")
        os.makedirs(os.path.join(self.output, "checkpoints/"), exist_ok=True)
        torch.save(
            {
                "tracking_net": self.net.state_dict(),
                "keyframe_timestamps": self.video.timestamp,
            },
            os.path.join(self.output, "checkpoints/go.ckpt"),
        )

    def terminate(self, processes: List, stream=None, gaussian_mapper_last_state=None):
        """fill poses for non-keyframe images and evaluate"""
        self.info("Initiating termination ...")

        # self.save_state()  ## this is not reached
        if self.do_evaluate:
            self.info("Doing evaluation!")
            self.evaluate(stream, gaussian_mapper_last_state=gaussian_mapper_last_state)
            self.info("Evaluation complete")

        for i, p in enumerate(processes):
            p.terminate()
            p.join()
            self.info("Terminated process {}".format(p.name))
        self.info("Terminate: Done!")

    def run(self, stream):
        processes = [
            # NOTE The OpenCV thread always needs to be 0 to work somehow
            mp.Process(target=self.show_stream, args=(0, self.input_pipe, self.cfg.show_stream), name="OpenCV Stream"),
            mp.Process(target=self.tracking, args=(1, stream, self.input_pipe), name="Frontend Tracking"),
            mp.Process(target=self.global_ba, args=(2, self.cfg.run_backend), name="Backend"),
            mp.Process(target=self.visualizing, args=(3, self.cfg.run_visualization), name="Visualizing"),
            mp.Process(
                target=self.gaussian_mapping,
                args=(4, self.cfg.run_mapping, self.mapping_queue, self.received_mapping),
                name="Gaussian Mapping",
            ),
            mp.Process(
                target=self.mapping_gui,
                args=(5, self.cfg.run_mapping_gui and self.cfg.run_mapping and not self.cfg.evaluate),
                name="Mapping GUI",
            ),
        ]

        self.num_running_thread[0] += len(processes)
        for p in processes:
            p.start()

        # Wait for all processes to have finished before terminating and for final mapping update to be transmitted
        if self.cfg.run_mapping:
            while self.mapping_queue.empty():
                pass
            # Receive the final update, so we can do something with it ...
            a = self.mapping_queue.get()
            self.info("Received final mapping update!")
            if a == "None":
                a = deepcopy(a)
                gaussian_mapper_last_state = None
            else:
                gaussian_mapper_last_state = clone_obj(a)
            del a  # NOTE Always delete receive object from a multiprocessing Queue!
            self.received_mapping.set()

        # Let the processes run until they are finished (When using GUI's these need to be closed manually)
        else:
            gaussian_mapper_last_state = None

        while self.all_finished < self.num_running_thread:
            pass

        self.terminate(processes, stream, gaussian_mapper_last_state)