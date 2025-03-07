"""
High-level wrapper functions for setting up the dataset and dataloaders.
"""
import json
import os
from copy import deepcopy
from typing import Dict, List, Literal

import numpy as np
import pandas as pd
import torch
from loguru import logger
from tqdm import tqdm

from dannce.config import _DEFAULT_SEG_MODEL
from dannce.engine.data import (dataset, generator, processing,
                                serve_data_DANNCE)
from dannce.engine.models.segmentation import get_instance_segmentation_model
from dannce.engine.utils.debug import write_debug
from dannce.engine.utils.experiment import (make_folder, set_device,
                                            set_random_seed)
from dannce.engine.utils.save import (save_params_pickle, save_params_yaml,
                                      write_com_file)
from dannce.engine.skeletons.utils import SYMMETRY, load_body_profile


def experiment_setup(
    params: Dict,
    mode: Literal["dannce_train", "dannce_predict", "com_train", "com_predict"],
):
    # Make the training directory if it does not exist.
    expdir = make_folder(f"{mode}_dir", params)

    # setup logger
    logger.add(
        f"{expdir}/stats_{mode}.log",
        format="{time:YYYY-MM-DD HH:mm} | {level} | {message}",
    )
    # deploy GPU devices
    device = set_device(params)

    # fix random seed if specified
    if params["random_seed"] is not None:
        set_random_seed(params["random_seed"])
        logger.info("***Fix random seed as {}***".format(params["random_seed"]))

    return logger, device


def set_dataset(params: Dict):
    if params["dataset"] == "rat7m":
        dataset_preparer = make_rat7m
    elif params["dataset"] == "rat7m+pair":
        dataset_preparer = make_rat7m
        spec_args = {**spec_args, "merge_pair": True}
    elif params["dataset"] == "pair":
        dataset_preparer = make_pair
    else:
        dataset_preparer = make_dataset

    return dataset_preparer


def make_dataset(
    params: Dict,
    base_params: Dict,
    shared_args: Dict,
    shared_args_train: Dict,
    shared_args_valid: Dict,
):
    # load in experiments from config file
    exps = params["exp"]
    num_experiments = len(exps)
    params["experiment"] = {}

    (
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        camnames,
        total_chunks,
        temporal_chunks,
    ) = processing.load_all_exps(params)

    # Additionally, to keep videos unique across experiments, need to add
    # experiment labels in other places. E.g. experiment 0 CameraE's "camname"
    # Becomes 0_CameraE. *NOTE* This function modified camnames in place
    # to add the appropriate experiment ID
    cameras, datadict, params = serve_data_DANNCE.prepend_experiment(
        params, datadict, num_experiments, camnames, cameras
    )

    # make train/valid splits
    partition = processing.make_data_splits(
        samples,
        params,
        params["dannce_train_dir"],
        num_experiments,
        temporal_chunks=temporal_chunks,
    )
    pairs = None
    if params["is_social_dataset"]:
        partition, pairs = processing.resplit_social(partition)

    # Check if there are any unlabeled samples
    samples = partition["train_sampleIDs"]
    unlabeled_samples = []
    for samp in samples:
        if np.isnan(datadict_3d[samp]).all():
            unlabeled_samples.append(samp)
    labeled_samples = list(set(samples) - set(unlabeled_samples))
    n_unlabeled = len(unlabeled_samples)
    n_labeled = len(labeled_samples)
    logger.info("***Train LABELED: UNLABELED = {}:{}***".format(n_labeled, n_unlabeled))

    if params["unlabeled_fraction"] != None:
        partition = processing.reselect_unlabeled_in_train(
            partition, datadict_3d, params["unlabeled_fraction"],
        )

    if params.get("social_big_volume", False):
        vmin, vmax = params["vmin"], params["vmax"]
        threshold = (vmax - vmin) / 2

        new_datadict3d, new_comdict3d = {}, {}
        new_partition = {}
        samples = []
        partition_names = ["train_sampleIDs", "valid_sampleIDs"]
        for i, (k, v) in enumerate(pairs.items()):
            samps = []
            for (vol1, vol2) in v:
                anchor1, anchor2 = com3d_dict[vol1], com3d_dict[vol2]
                pose3d1, pose3d2 = datadict_3d[vol1], datadict_3d[vol2]
                anchor1, anchor2 = anchor1[:, np.newaxis], anchor2[:, np.newaxis]
                dist = np.sqrt(np.sum((anchor1 - anchor2) ** 2))
                # if two COMs are close enough
                if dist <= threshold:
                    # replace w/ their averaged position
                    new_com = (anchor1 + anchor2) / 2
                    primary = pose3d1 if anchor1[0] < anchor2[0] else pose3d2
                    secondary = pose3d2 if anchor1[0] < anchor2[0] else pose3d1
                    # discard unneeded sampleIDs and get correct pose 3d
                    new_pose3d = np.concatenate(
                        (primary, secondary), axis=-1
                    )  # [3, 46]
                    new_datadict3d[vol1] = processing.mask_coords_outside_volume(
                        vmin - threshold / 2, vmax + threshold / 2, new_pose3d, new_com
                    )
                    new_comdict3d[vol1] = np.squeeze(new_com)
                    samps.append(vol1)
            new_partition[partition_names[i]] = samps
            samples += samps

        datadict_3d = new_datadict3d
        com3d_dict = new_comdict3d
        samples = np.array(sorted(samples))
        partition = new_partition
        pairs = None

        params["n_channels_out"] *= 2
        base_params["n_channels_out"] *= 2
        params["vmin"] = vmin - threshold / 2
        params["vmax"] = vmax + threshold / 2
        base_params["vmin"] = params["vmin"]
        base_params["vmax"] = params["vmax"]
        params["is_social_dataset"] = False

    if params.get("social_joint_training", False):
        # OPTION1: only choose volumes with both animals present
        # partition, com3d_dict, datadict_3d, samples = processing.filter_com3ds(pairs, com3d_dict, datadict_3d)
        # OPTION2: also predict on the other animal if any of its joints is also in the volume
        datadict_3d = processing.prepare_joint_volumes(
            params, pairs, com3d_dict, datadict_3d
        )

        # params["is_social_dataset"] = False
        params["n_channels_out"] *= 2
        base_params["n_channels_out"] *= 2

        pairs = None
        params["is_social_dataset"] = False

    # Dump the params into file for reproducibility
    save_params_pickle(params)
    # Save a yaml copy for direct check
    save_params_yaml(params)

    # Setup additional variables for later use
    tifdirs = []  # Training from single images not yet supported in this demo

    # Two possible data loading schemes:
    # (i) Directly use pre-saved npy volumes
    # (ii) Load images from video files into memory and generate the samples needed for training
    vid_exps = np.arange(num_experiments)

    # initialize needed videos
    vids = processing.initialize_all_vids(params, datadict, vid_exps, pathonly=True)

    base_params = {
        **base_params,
        "camnames": camnames,
        "vidreaders": vids,
        "chunks": total_chunks,
    }

    # compute segment length priors from data if specified
    if 'BoneLengthLoss' in params['loss']:
        if params['loss']['BoneLengthLoss'].get('compute_priors_from_data', False):
            pose3d = deepcopy(np.stack(list(datadict_3d.values()), axis=0))
            pose3d = np.transpose(pose3d, (0, 2, 1))  # [N, n_kpts, 3]
            
            prior_path = compute_segment_length_priors(params, pose3d)
            logger.debug(f"Applying segment priors computed from n={len(pose3d)} training samples ({prior_path}) instead of loading from file. Check if this behavior is desired.")

    # Prepare datasets and dataloaders
    makedata_func = _make_data_npy if params["use_npy"] else _make_data_mem
    train_generator, valid_generator = makedata_func(
        params,
        base_params,
        shared_args,
        shared_args_train,
        shared_args_valid,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        camnames,
        samples,
        partition,
        pairs,
        tifdirs,
        vids,
    )
    logger.info(
        "***TRAIN:VALIDATION = {}:{}***".format(
            len(train_generator), len(valid_generator)
        )
    )
    train_dataloader, valid_dataloader = serve_data_DANNCE.setup_dataloaders(
        train_generator, valid_generator, params
    )

    return train_dataloader, valid_dataloader, params["n_views"]  # len(camnames[0])


def _convert_rat7m_to_label3d(
    annot_dict: Dict, all_exps: List[str],
):
    camnames = annot_dict["camera_names"]

    samples = []
    datadict, datadict_3d, com3d_dict = {}, {}, {}
    cameras = {}

    all_data = annot_dict["table"]
    print("** Loading in RAT7M annotation **")
    for i, expname in enumerate(tqdm(all_exps)):
        subject_idx, day_idx = expname.split("-")
        subject_idx, day_idx = int(subject_idx[-1]), int(day_idx[-1])

        data_idx = np.where(
            (all_data["subject_idx"] == subject_idx) & (all_data["day_idx"] == day_idx)
        )[0]

        sampleIDs = [
            str(i) + "_" + str(frame)
            for frame in all_data["frame_idx"][camnames[0]][data_idx]
        ]
        samples += sampleIDs

        data3d = np.transpose(
            all_data["3D_keypoints"][data_idx], (0, 2, 1)
        )  # [N, 3, 20]
        com3d = np.mean(data3d, axis=-1)  # [N, 3]
        data2d, frames = [], []

        for cam in camnames:
            data2d_cam = np.transpose(
                all_data["2D_keypoints"][cam][data_idx], (0, 2, 1)
            )
            frames_cam = all_data["frame_idx"][cam][data_idx]
            data2d.append(data2d_cam)
            frames.append(frames_cam)

        for j, samp in enumerate(sampleIDs):
            data, frame = {}, {}
            for camidx, camname in enumerate(camnames):
                data[str(i) + "_" + camname] = data2d[camidx][j]
                frame[str(i) + "_" + camname] = frames[camidx][j]

            datadict[samp] = {"data": data, "frames": frame}
            datadict_3d[samp] = data3d[j]
            com3d_dict[samp] = com3d[j]

        # prepare cameras
        cameras[i] = {}
        for camname in camnames:
            new_params = {}
            old_params = annot_dict["cameras"][subject_idx][day_idx][camname]
            new_params["K"] = old_params["IntrinsicMatrix"]
            new_params["R"] = old_params["rotationMatrix"]
            new_params["t"] = old_params["translationVector"]
            new_params["RDistort"] = old_params["RadialDistortion"]
            new_params["TDistort"] = old_params["TangentialDistortion"]
            cameras[i][str(i) + "_" + camname] = new_params

    samples = np.array(samples)
    return samples, datadict, datadict_3d, com3d_dict, camnames, cameras


def make_rat7m(
    params,
    base_params,
    shared_args,
    shared_args_train,
    shared_args_valid,
    root="datasets/rat7m",
    annot="final_annotations_w_correct_clusterIDs.pkl",
    viddir="videos_concat",
    merge_pair=False,
    merge_label3d=False,
):
    # load annotations from disk
    annot_dict = np.load(os.path.join(root, annot), allow_pickle=True)

    # convert everything to label3d format
    experiments_train = ["s1-d1", "s2-d1", "s2-d2", "s3-d1", "s4-d1"]
    experiments_test = ["s5-d1", "s5-d2"]
    all_exps = experiments_train + experiments_test
    num_experiments = len(all_exps)
    params["experiment"] = {}

    (
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        camnames,
        cameras,
    ) = _convert_rat7m_to_label3d(annot_dict, all_exps)
    temporal_chunks = {}

    # Use the camnames to find the chunks for each video
    all_vids = [f for f in os.listdir(os.path.join(root, viddir)) if f.endswith(".mp4")]
    vids = {}
    total_chunks = {}
    print("** Preparing video readers **")
    for e in tqdm(range(num_experiments)):
        for name in camnames:
            video_files = [
                f for f in all_vids if all_exps[e] + "-" + name.replace("C", "c") in f
            ]
            video_files = sorted(
                video_files, key=lambda x: int(x.split("-")[-1].split(".mp4")[0])
            )
            total_chunks[str(e) + "_" + name] = np.sort(
                [int(x.split("-")[-1].split(".mp4")[0]) for x in video_files]
            )
            vids[str(e) + "_" + name] = {}
            for file in video_files:
                vids[str(e) + "_" + name][
                    str(e) + "_" + name + "/0.mp4"
                ] = os.path.join(root, viddir, file)

    new_camnames = {}
    for e in range(len(all_exps)):
        new_camnames[e] = [str(e) + "_" + camname for camname in camnames]
    camnames = new_camnames

    # make train/valid splits
    partition = processing.make_data_splits(
        samples,
        params,
        params["dannce_train_dir"],
        num_experiments,
        temporal_chunks=temporal_chunks,
    )
    pairs = None
    if merge_pair:
        # fix random seed
        np.random.seed(10241024)
        partition["train_sampleIDs"] = np.random.choice(
            partition["train_sampleIDs"], 2000
        )

    # Dump the params into file for reproducibility
    save_params_pickle(params)

    # Setup additional variables for later use
    tifdirs = []  # Training from single images not yet supported in this demo
    # vid_exps = np.arange(num_experiments)

    # initialize needed videos
    # vids = processing.initialize_all_vids(params, datadict, vid_exps, pathonly=True)

    base_params = {
        **base_params,
        "camnames": camnames,
        "vidreaders": vids,
        "chunks": total_chunks,
    }

    # Prepare datasets and dataloaders
    NPY_DIRNAMES = ["image_volumes", "grid_volumes", "targets"]

    TO_BE_EXAMINED = NPY_DIRNAMES
    npydir, missing_npydir = {}, {}

    for e, name in enumerate(all_exps):
        # for social, cannot use the same default npy volume dir for both animals
        npy_folder = os.path.join(root, "npy_volumes", name)
        npydir[e] = npy_folder

        # create missing npy directories
        if not os.path.exists(npydir[e]):
            missing_npydir[e] = npydir[e]
            for dir in TO_BE_EXAMINED:
                os.makedirs(os.path.join(npydir[e], dir))
        else:
            for dir in TO_BE_EXAMINED:
                dirpath = os.path.join(npydir[e], dir)
                if (not os.path.exists(dirpath)) or (len(os.listdir(dirpath)) == 0):
                    missing_npydir[e] = npydir[e]
                    os.makedirs(dirpath, exist_ok=True)

    missing_samples = [
        samp
        for samp in samples
        if int(samp.split("_")[0]) in list(missing_npydir.keys())
    ]

    # check any other missing npy samples
    for samp in list(set(samples) - set(missing_samples)):
        e, sampleID = int(samp.split("_")[0]), samp.split("_")[1]
        if not os.path.exists(
            os.path.join(npydir[e], "image_volumes", f"0_{sampleID}.npy")
        ):
            missing_samples.append(samp)
            missing_npydir[e] = npydir[e]

    missing_samples = np.array(sorted(missing_samples))

    train_generator, valid_generator = _make_data_npy(
        params,
        base_params,
        shared_args,
        shared_args_train,
        shared_args_valid,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        camnames,
        samples,
        partition,
        pairs,
        tifdirs,
        vids,
        rat7m=True,
        rat7m_npy=[npydir, missing_npydir, missing_samples],
    )

    if merge_pair:
        train_generator_pair, valid_generator_pair, _ = make_pair(
            params,
            base_params,
            shared_args,
            shared_args_train,
            shared_args_valid,
            merge_pair=True,
        )
        train_generator = torch.utils.data.ConcatDataset(
            [train_generator, train_generator_pair.dataset]
        )
        valid_generator = valid_generator_pair.dataset

    if merge_label3d:
        train_generator_label3d, valid_generator_label3d, _ = make_dataset(
            params, base_params, shared_args, shared_args_train, shared_args_valid,
        )
        train_generator = torch.utils.data.ConcatDataset(
            [train_generator, train_generator_label3d.dataset]
        )

    train_dataloader, valid_dataloader = serve_data_DANNCE.setup_dataloaders(
        train_generator, valid_generator, params
    )

    return train_dataloader, valid_dataloader, len(camnames[0])


def sample_COM_augmentation(
    datadict: Dict,
    datadict_3d: Dict,
    com3d_dict: Dict,
    partition: Dict,
    perturb_radius: int = 20,
    aug_iters: int = 1,
):
    """Create augmented volumes by perturbing positions of the center of mass (COM)/anchors. 
    This is only performed over labeled samples in the training set.
    """

    train_samples = list(partition["train_sampleIDs"])
    valid_samples = list(partition["valid_sampleIDs"])
    train_samples_new = []
    for iter in range(aug_iters):
        pbar = tqdm(train_samples)
        for sample in pbar:
            # Only augment training samples
            if sample not in train_samples:
                continue

            # Only augment labeled samples
            if np.isnan(datadict_3d[sample]).all():
                continue

            com3d_new = deepcopy(com3d_dict[sample])
            com_aug = (
                perturb_radius * 2 * np.random.rand(len(com3d_new)) - perturb_radius
            )
            com3d_new += com_aug
            # Embed the used COM augmentation in sampleID?
            # sample_new = sample+"-aug{}".format('_'.join(str(coord) for coord in com_aug))
            sample_new = sample + "-comaug{}".format(iter)
            train_samples_new.append(sample_new)
            com3d_dict[sample_new] = com3d_new
            datadict_3d[sample_new] = datadict_3d[sample]
            datadict[sample_new] = datadict[sample]

    logger.info("Added {} COM augmented samples.".format(len(train_samples_new)))
    partition["train_sampleIDs"] = np.array(sorted(train_samples + train_samples_new))
    samples = np.array(sorted(train_samples + train_samples_new + valid_samples))
    return samples, datadict, datadict_3d, com3d_dict, partition


def _make_data_npy(
    params,
    base_params,
    shared_args,
    shared_args_train,
    shared_args_valid,
    datadict,
    datadict_3d,
    com3d_dict,
    cameras,
    camnames,
    samples,
    partition,
    pairs,
    tifdirs,
    vids,
    rat7m=False,
    rat7m_npy=None,
):
    """
    Pre-generate the volumetric data and save as .npy.
    Good for large training set that is unable to fit in memory.
    Can be reused for future experiments.
    """
    if rat7m:
        assert rat7m_npy is not None
        npydir, missing_npydir, missing_samples = rat7m_npy
        missing_samples = np.array(sorted(missing_samples))
    else:
        # Populate with COM augmented samples if needed
        if params["COM_augmentation"]:
            (
            samples,
                datadict,
                datadict_3d,
                com3d_dict,
                partition,
            ) = sample_COM_augmentation(
                datadict,
                datadict_3d,
                com3d_dict,
                partition,
                perturb_radius=params["COM_aug_radius"],
                aug_iters=params["COM_aug_iters"],
            )

        # Examine through experiments for missing npy data files
        (
            npydir,
            missing_npydir,
            missing_samples,
        ) = serve_data_DANNCE.examine_npy_training(params, samples)

    if len(missing_samples) != 0:
        logger.info(
            "{} npy files for experiments {} are missing.".format(
                len(missing_samples), list(missing_npydir.keys())
            )
        )
    else:
        logger.info("No missing npy files. Ready for training.")

    # Generate missing npy files
    # mono conversion will happen from RGB npy files, and the generator
    # needs to be aware that the npy files contain RGB content
    params["chan_num"] = params["n_channels_in"]
    spec_params = {
        "channel_combo": None,
        "predict_flag": False,
        "norm_im": False,
        "expval": True,
    }

    genfunc = generator.DataGenerator_3Dconv
    if params["is_social_dataset"]:
        spec_params["occlusion"] = params["downscale_occluded_view"]
        genfunc = generator.DataGenerator_3Dconv_social

    valid_params = {**base_params, **spec_params}

    if len(missing_samples) != 0:
        npy_generator = genfunc(
            missing_samples,
            datadict,
            datadict_3d,
            cameras,
            missing_samples,
            com3d_dict,
            tifdirs,
            **valid_params,
        )
        processing.save_volumes_into_npy(
            params, npy_generator, missing_npydir, samples,
        )
        
        npy_generator.close_all_readers()

    # generate segmentation masks if needed
    segmentation_model, valid_params_sil = get_segmentation_model(
        params, valid_params, vids
    )

    if segmentation_model is not None:
        (
            npydir,
            missing_npydir,
            missing_samples,
        ) = serve_data_DANNCE.examine_npy_training(params, samples, aux=True)

        if len(missing_samples) != 0:
            logger.info(
                "{} aux npy files for experiments {} are missing.".format(
                    len(missing_samples), list(missing_npydir.keys())
                )
            )
            npy_generator = genfunc(
                missing_samples,
                datadict,
                datadict_3d,
                cameras,
                missing_samples,
                com3d_dict,
                tifdirs,
                segmentation_model=segmentation_model,
                **valid_params_sil,
            )
            processing.save_volumes_into_npy(
                params, npy_generator, missing_npydir, samples, silhouette=True
            )
        else:
            logger.info("No missing aux npy files. Ready for training.")

    # Use another data generator class for applying augmentation
    genfunc = dataset.PoseDatasetNPY
    args_train = {
        "list_IDs": partition["train_sampleIDs"],
        "labels_3d": datadict_3d,
        "npydir": npydir,
    }
    args_train = {
        **args_train,
        **shared_args_train,
        **shared_args,
        "sigma": params["sigma"],
        "mono": params["mono"],
        "aux_labels": None,
        "aux": params["use_silhouette"],
        "temporal_chunk_list": partition["train_chunks"]
        if params["use_temporal"]
        else None,
    }

    args_valid = {
        "list_IDs": partition["valid_sampleIDs"],
        "labels_3d": datadict_3d,
        "npydir": npydir,
        "aux_labels": None,
        "aux": params["use_silhouette"],
    }
    args_valid = {
        **args_valid,
        **shared_args_valid,
        **shared_args,
        "sigma": params["sigma"],
        "mono": params["mono"],
        "temporal_chunk_list": partition["valid_chunks"]
        if params["use_temporal"]
        else None,
    }

    # if params["is_social_dataset"]:
    #     args_train = {**args_train, "pairs": pairs["train_pairs"]}
    #     args_valid = {**args_valid, "pairs": pairs["valid_pairs"]}

    # initialize datasets and dataloaders
    train_generator = genfunc(**args_train)
    valid_generator = genfunc(**args_valid)

    return train_generator, valid_generator


def _make_data_mem(
    params: Dict,
    base_params: Dict,
    shared_args: Dict,
    shared_args_train: Dict,
    shared_args_valid: Dict,
    datadict: Dict,
    datadict_3d: Dict,
    com3d_dict: Dict,
    cameras: Dict,
    camnames: List,
    samples: List,
    partition: Dict,
    pairs: Dict,
    tifdirs: List,
    vids,
):
    """
    Training samples are stored in the memory and deployed on the fly.
    """
    n_cams = len(camnames[0])

    genfunc = (
        generator.DataGenerator_3Dconv_social
        if params["is_social_dataset"]
        else generator.DataGenerator_3Dconv
    )

    # Populate with COM augmented samples if needed
    if params["COM_augmentation"]:
        (
            samples,
            datadict,
            datadict_3d,
            com3d_dict,
            partition,
        ) = sample_COM_augmentation(
            datadict, datadict_3d, com3d_dict, partition,
            perturb_radius=params["COM_aug_radius"],
            aug_iters=params["COM_aug_iters"],
        )

    # Used to initialize arrays for mono, and also in *frommem (the final generator)
    params["chan_num"] = 1 if params["mono"] else params["n_channels_in"]

    spec_params = {
        "channel_combo": params["channel_combo"],
        "expval": params["expval"],
    }

    valid_params = {**base_params, **spec_params}

    # Setup a generator that will read videos and labels
    train_gen_params = [
        partition["train_sampleIDs"],
        datadict,
        datadict_3d,
        cameras,
        partition["train_sampleIDs"],
        com3d_dict,
        tifdirs,
    ]
    valid_gen_params = [
        partition["valid_sampleIDs"],
        datadict,
        datadict_3d,
        cameras,
        partition["valid_sampleIDs"],
        com3d_dict,
        tifdirs,
    ]

    train_generator = genfunc(*train_gen_params, **valid_params)
    valid_generator = genfunc(*valid_gen_params, **valid_params)

    # load everything into memory
    X_train, X_train_grid, y_train = processing.load_volumes_into_mem(
        params,
        partition,
        n_cams,
        train_generator,
        train=True,
        social=params["is_social_dataset"],
    )
    X_valid, X_valid_grid, y_valid = processing.load_volumes_into_mem(
        params,
        partition,
        n_cams,
        valid_generator,
        train=False,
        social=params["is_social_dataset"],
    )

    segmentation_model, valid_params_sil = get_segmentation_model(
        params, valid_params, vids
    )

    y_train_aux, y_valid_aux = None, None
    if segmentation_model is not None:
        train_generator_sil = genfunc(
            *train_gen_params, **valid_params_sil, segmentation_model=segmentation_model
        )

        valid_generator_sil = genfunc(
            *valid_gen_params, **valid_params_sil, segmentation_model=segmentation_model
        )

        _, _, y_train_aux = processing.load_volumes_into_mem(
            params,
            partition,
            n_cams,
            train_generator_sil,
            train=True,
            silhouette=True,
            social=params["is_social_dataset"],
        )
        _, _, y_valid_aux = processing.load_volumes_into_mem(
            params,
            partition,
            n_cams,
            valid_generator_sil,
            train=False,
            silhouette=True,
            social=params["is_social_dataset"],
        )

        if params["use_silhouette_in_volume"]:
            # concatenate RGB image volumes with silhouette volumes
            X_train = np.concatenate(
                (X_train, y_train_aux, y_train_aux, y_train_aux), axis=-1
            )
            X_valid = np.concatenate(
                (X_valid, y_valid_aux, y_valid_aux, y_valid_aux), axis=-1
            )
            logger.info("Input dimension is now {}".format(X_train.shape))

            params["use_silhouette"] = False
            logger.info("Turn off silhouette loss.")
            y_train_aux, y_valid_aux = None, None

    if params["is_social_dataset"]:
        X_train, X_train_grid, y_train, y_train_aux = processing.align_social_data(
            X_train, X_train_grid, y_train, y_train_aux
        )
        X_valid, X_valid_grid, y_valid, y_valid_aux = processing.align_social_data(
            X_valid, X_valid_grid, y_valid, y_valid_aux
        )

    genfunc = dataset.PoseDatasetFromMem
    args_train = {
        "list_IDs": np.arange(len(partition["train_sampleIDs"])),
        "data": X_train,
        "labels": y_train,
    }
    args_train = {
        **args_train,
        **shared_args_train,
        **shared_args,
        "xgrid": X_train_grid,
        "aux_labels": y_train_aux,
        "temporal_chunk_list": partition["train_chunks"]
        if params["use_temporal"]
        else None,
    }

    args_valid = {
        "list_IDs": np.arange(len(partition["valid_sampleIDs"])),
        "data": X_valid,
        "labels": y_valid,
        "aux_labels": y_valid_aux,
    }
    args_valid = {
        **args_valid,
        **shared_args_valid,
        **shared_args,
        "xgrid": X_valid_grid,
        "temporal_chunk_list": partition["valid_chunks"]
        if params["use_temporal"]
        else None,
    }

    if params["is_social_dataset"]:
        args_train = {**args_train, "pairs": pairs["train_pairs"]}
        args_valid = {**args_valid, "pairs": pairs["valid_pairs"]}

    # initialize datasets and dataloaders
    train_generator = genfunc(**args_train)
    valid_generator = genfunc(**args_valid)

    return train_generator, valid_generator


def get_segmentation_model(params, valid_params, vids):
    valid_params_sil = deepcopy(valid_params)
    valid_params_sil["vidreaders"] = vids  # vids_sil

    # ensure voxel values stay positive, allowing conversion to binary below
    valid_params_sil["norm_im"] = False
    valid_params_sil["expval"] = True

    if not params["use_silhouette"]:
        return None, valid_params_sil

    # prepare segmentation model
    segmentation_model = get_instance_segmentation_model(num_classes=2)
    checkpoints = torch.load(_DEFAULT_SEG_MODEL)["state_dict"]
    segmentation_model.load_state_dict(checkpoints)
    segmentation_model.eval()
    segmentation_model = segmentation_model.to("cuda:0")

    return segmentation_model, valid_params_sil


def make_dataset_inference(params, valid_params):
    # The library is configured to be able to train over multiple animals ("experiments")
    # at once. Because supporting code expects to see an experiment ID# prepended to
    # each of these data keys, we need to add a token experiment ID here.
    samples = []
    datadict = {}
    datadict_3d = {}
    com3d_dict = {}
    cameras = {}
    camnames = {}

    num_experiments = len(params["experiment"])
    for e in range(num_experiments):
        (
            params["experiment"][e],
            samples_,
            datadict_,
            datadict_3d_,
            cameras_,
            com3d_dict_,
            _,
        ) = processing.do_COM_load(
            params["experiment"][e], params["experiment"][e], e, params, training=False,
        )

        # Write 3D COM to file. This might be different from the input com3d file
        # if arena thresholding was applied.
        if e == 0:
            write_com_file(params, samples_, com3d_dict_)

        (
            samples,
            datadict,
            datadict_3d,
            com3d_dict,
            _,
        ) = serve_data_DANNCE.add_experiment(
            e,
            samples,
            datadict,
            datadict_3d,
            com3d_dict,
            samples_,
            datadict_,
            datadict_3d_,
            com3d_dict_,
        )

        cameras[e] = cameras_
        camnames[e] = params["experiment"][e]["camnames"]

    # Need a '0' experiment ID to work with processing functions.
    # *NOTE* This function modified camnames in place
    # to add the appropriate experiment ID
    cameras, datadict, params = serve_data_DANNCE.prepend_experiment(
        params, datadict, num_experiments, camnames, cameras, dannce_prediction=True
    )

    # Initialize video dictionary. paths to videos only.
    # TODO: Remove this immode option if we decide not
    # to support tifs
    if params["immode"] == "vid":
        vids = {}
        for e in range(num_experiments):
            vids = processing.initialize_vids(params, datadict, 0, vids, pathonly=True)

    # Parameters
    valid_params = {
        **valid_params,
        "camnames": camnames,
        "vidreaders": vids,
        "chunks": params["chunks"],
    }

    # Datasets
    if params["is_social_dataset"]:
        for sample in samples:
            expID, sampleID = sample.split("_")
            if int(expID) != 0:
                continue
            for i in range(1, params["n_instances"]):
                new_sample = f"{int(i)}_{sampleID}"
                com3d_dict[new_sample] = com3d_dict[sample][:, i]
            com3d_dict[sample] = com3d_dict[sample][:, 0]

    samples = np.array(samples)
    valid_inds = np.arange(len(samples))
    partition = {"valid_sampleIDs": samples[valid_inds]}

    # TODO: Remove tifdirs arguments, which are deprecated
    tifdirs = []

    # Generators
    genfunc = (
        generator.DataGenerator_3Dconv_social
        if params["is_social_dataset"]
        else generator.DataGenerator_3Dconv
    )

    predict_params = [
        partition["valid_sampleIDs"],
        datadict,
        datadict_3d,
        cameras,
        partition["valid_sampleIDs"],
        com3d_dict,
        tifdirs,
    ]

    spec_params = {}
    if params["is_social_dataset"]:
        spec_params["n_instances"] = params["n_instances"]

    predict_generator = genfunc(*predict_params, **valid_params, **spec_params)

    predict_generator_sil = None
    if (params["use_silhouette_in_volume"]) or (
        params["write_visual_hull"] is not None
    ):
        # require silhouette + RGB volume
        vids_sil = processing.initialize_vids(
            params, datadict, 0, {}, pathonly=True, vidkey="viddir_sil"
        )
        valid_params_sil = deepcopy(valid_params)
        valid_params_sil["vidreaders"] = vids_sil
        valid_params_sil["norm_im"] = False
        valid_params_sil["expval"] = True

        predict_generator_sil = generator.DataGenerator_3Dconv(
            *predict_params, **valid_params_sil
        )

    return predict_generator, predict_generator_sil, camnames, partition


def make_data_com(params, train_params, valid_params):
    if params["com_exp"] is not None:
        exps = params["com_exp"]
    else:
        exps = params["exp"]

    num_experiments = len(exps)
    (
        samples,
        datadict,
        datadict_3d,
        cameras,
        camnames,
        total_chunks,
    ) = processing.load_all_com_exps(params, exps)

    cameras, datadict, params = serve_data_DANNCE.prepend_experiment(
        params, datadict, num_experiments, camnames, cameras
    )

    # make train/valid splits
    partition = processing.make_data_splits(
        samples, params, params["com_train_dir"], num_experiments
    )

    # Initialize video objects
    vids = {}
    for e in range(num_experiments):
        vids = processing.initialize_vids(params, datadict, e, vids, pathonly=True)

    train_params = {
        **train_params,
        "camnames": camnames,
        "chunks": total_chunks,
    }

    valid_params = {
        **valid_params,
        "camnames": camnames,
        "chunks": total_chunks,
    }

    # Set up generators
    labels = datadict
    dh = (params["crop_height"][1] - params["crop_height"][0]) // params["downfac"]
    dw = (params["crop_width"][1] - params["crop_width"][0]) // params["downfac"]
    params["input_shape"] = (dh, dw)
    # effective n_channels, which is different if using a mirror arena configuration
    eff_n_channels_out = (
        len(camnames[0]) if params["mirror"] else params["n_channels_out"]
    )
    if params["mirror"]:
        ncams = 1  # Effectively, for the purpose of batch indexing
    else:
        ncams = len(camnames[0])

    train_generator = generator.DataGenerator_COM(
        params["n_instances"],
        partition["train_sampleIDs"],
        labels,
        vids,
        **train_params,
    )
    valid_generator = generator.DataGenerator_COM(
        params["n_instances"],
        partition["valid_sampleIDs"],
        labels,
        vids,
        **valid_params,
    )

    logger.info("Loading data")
    ims_train = np.zeros(
        (ncams * len(partition["train_sampleIDs"]), dh, dw, params["chan_num"],),
        dtype="float32",
    )
    y_train = np.zeros(
        (ncams * len(partition["train_sampleIDs"]), dh, dw, eff_n_channels_out),
        dtype="float32",
    )
    ims_valid = np.zeros(
        (ncams * len(partition["valid_sampleIDs"]), dh, dw, params["chan_num"],),
        dtype="float32",
    )
    y_valid = np.zeros(
        (ncams * len(partition["valid_sampleIDs"]), dh, dw, eff_n_channels_out),
        dtype="float32",
    )

    for i in tqdm(range(len(partition["train_sampleIDs"]))):
        ims = train_generator.__getitem__(i)
        ims_train[i * ncams : (i + 1) * ncams] = ims[0]
        y_train[i * ncams : (i + 1) * ncams] = ims[1]

    for i in tqdm(range(len(partition["valid_sampleIDs"]))):
        ims = valid_generator.__getitem__(i)
        ims_valid[i * ncams : (i + 1) * ncams] = ims[0]
        y_valid[i * ncams : (i + 1) * ncams] = ims[1]

    write_debug(params, ims_train, ims_valid, y_train)

    train_generator.close_all_readers()
    valid_generator.close_all_readers()

    train_generator = dataset.COMDatasetFromMem(
        np.arange(ims_train.shape[0]),
        ims_train,
        y_train,
        batch_size=ncams,
        augment_hue=params["augment_hue"],
        augment_brightness=params["augment_brightness"],
        augment_rotation=params["augment_rotation"],
        augment_shear=params["augment_hue"],
        augment_shift=params["augment_brightness"],
        augment_zoom=params["augment_rotation"],
        bright_val=params["augment_bright_val"],
        hue_val=params["augment_hue_val"],
        shift_val=params["augment_shift_val"],
        rotation_val=params["augment_rotation_val"],
        shear_val=params["augment_shear_val"],
        zoom_val=params["augment_zoom_val"],
        chan_num=params["chan_num"],
    )
    valid_generator = dataset.COMDatasetFromMem(
        np.arange(ims_valid.shape[0]),
        ims_valid,
        y_valid,
        batch_size=ncams,
        shuffle=False,
        chan_num=params["chan_num"],
    )

    logger.info(f'***TRAIN:VALIDATION = {ims_train.shape[0]}:{ims_valid.shape[0]}***')

    def collate_fn(batch):
        X = torch.cat([item[0] for item in batch], dim=0)
        y = torch.cat([item[1] for item in batch], dim=0)

        return X, y

    train_dataloader = torch.utils.data.DataLoader(
        train_generator,
        batch_size=params["batch_size"],
        shuffle=True,
        collate_fn=collate_fn,
        num_workers=1,
    )
    valid_dataloader = torch.utils.data.DataLoader(
        valid_generator,
        batch_size=1,
        shuffle=False,
        collate_fn=collate_fn,
        num_workers=1,
    )

    return train_dataloader, valid_dataloader


def make_dataset_com_inference(params, predict_params):
    (
        samples,
        datadict,
        datadict_3d,
        cameras,
        camera_mats,
    ) = serve_data_DANNCE.prepare_data(params, stage="predict", return_cammat=True,)

    # Zero any negative frames
    for key in datadict.keys():
        for key_ in datadict[key]["frames"].keys():
            if datadict[key]["frames"][key_] < 0:
                datadict[key]["frames"][key_] = 0

    # The generator expects an experimentID in front of each sample key
    samples = ["0_" + str(f) for f in samples]
    datadict_ = {}
    for key in datadict.keys():
        datadict_["0_" + str(key)] = datadict[key]

    datadict = datadict_

    # Initialize video dictionary. paths to videos only.
    vids = {}
    vids = processing.initialize_vids(params, datadict, 0, vids, pathonly=True)

    partition = {}
    partition["valid_sampleIDs"] = samples
    labels = datadict

    predict_generator = generator.DataGenerator_COM(
        params["n_instances"],
        partition["valid_sampleIDs"],
        labels,
        vids,
        **predict_params,
    )

    return predict_generator, params, partition, camera_mats, cameras, datadict


def _convert_pair_to_label3d(
    root,
    metadata,
    camnames,
    experiments,
    total_n,
    count,
    samples,
    datadict,
    datadict_3d,
    com3d_dict,
    cameras,
    merge_pair=False,
):
    exps = []
    for exp in tqdm(experiments):
        sr1, sr2 = exp.split("_")
        filter = (
            metadata["FolderPath"].str.contains(sr1)
            & metadata["FolderPath"].str.contains(sr2)
            & (~metadata["FolderPath"].str.contains("m"))
        )  # exclude markerless animals
        candidates = metadata["FolderPath"][filter][:2]
        n_candidates = len(candidates)
        print(candidates)
        for can in candidates:
            exps.append(can)
            data = pd.read_csv(os.path.join(root, can, "markerDataset.csv"))
            good1 = data["goodFrame_an1"] == 1
            good2 = data["goodFrame_an2"] == 1
            good = good1 & good2
            if sum(good) == 0:
                print(f"No frames available for training in {can}")
                n_candidates -= 1
                continue

            pose3d1 = data.loc[
                :, data.columns.str.contains("absolutePosition_an1")
            ].to_numpy()
            pose3d2 = data.loc[
                :, data.columns.str.contains("absolutePosition_an2")
            ].to_numpy()
            pose3d1 = np.transpose(
                np.reshape(pose3d1, (pose3d1.shape[0], -1, 3)), (0, 2, 1)
            )
            pose3d2 = np.transpose(
                np.reshape(pose3d2, (pose3d2.shape[0], -1, 3)), (0, 2, 1)
            )
            if merge_pair:
                pair_index = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13])
                pose3d1_r7m = np.ones((*pose3d1.shape[:2], 20)) * np.nan
                pose3d1_r7m[:, :, pair_index] = pose3d1
                pose3d1 = pose3d1_r7m
                pose3d2_r7m = np.ones((*pose3d2.shape[:2], 20)) * np.nan
                pose3d2_r7m[:, :, pair_index] = pose3d2
                pose3d2 = pose3d2_r7m
            com3d1 = data.loc[
                :, data.columns.str.contains("centerOfmass_an1")
            ].to_numpy()
            com3d2 = data.loc[
                :, data.columns.str.contains("centerOfmass_an2")
            ].to_numpy()

            frames = np.arange(len(good))[
                good
            ]  # some videos do not have complete sets of frames, remove last second for avoiding issues
            frames = frames[frames < (len(good) - 120)]
            frames = np.random.choice(
                frames, total_n // n_candidates
            )  # will choose the same frame for both animals

            sampleIDs_an1 = [str(count) + "_" + str(frame) for frame in frames]
            sampleIDs_an2 = [str(count + 1) + "_" + str(frame) for frame in frames]
            samples += sampleIDs_an1
            samples += sampleIDs_an2

            for j, samp in enumerate(sampleIDs_an1):
                data, frame = {}, {}
                for camidx, camname in enumerate(camnames):
                    data[str(count) + "_" + camname] = (
                        np.ones((2, pose3d1.shape[1])) * np.nan
                    )
                    frame[str(count) + "_" + camname] = frames[j]

                datadict[samp] = {"data": data, "frames": frame}
                datadict_3d[samp] = pose3d1[frames][j]
                com3d_dict[samp] = com3d1[frames][j]

            for j, samp in enumerate(sampleIDs_an2):
                data, frame = {}, {}
                for camidx, camname in enumerate(camnames):
                    data[str(count + 1) + "_" + camname] = (
                        np.ones((2, pose3d1.shape[1])) * np.nan
                    )
                    frame[str(count + 1) + "_" + camname] = frames[j]

                datadict[samp] = {"data": data, "frames": frame}
                datadict_3d[samp] = pose3d2[frames][j]
                com3d_dict[samp] = com3d2[frames][j]

            # load camera information
            cameras[count] = {}
            cameras[count + 1] = {}
            for i in range(1, 7):
                campath = os.path.join(
                    root, can, "calibration", f"camera{i}_calibration.json"
                )
                camparam = json.load(open(campath))
                newparam = {}
                newparam["K"] = np.array(camparam["intrinsicMatrix"])
                newparam["R"] = np.array(camparam["rotationMatrix"])
                t = np.array(camparam["translationVector"]).T
                newparam["t"] = t[np.newaxis, :]
                newparam["RDistort"] = np.array(camparam["radialDistortion"]).T
                newparam["TDistort"] = np.array(camparam["tangentialDistortion"]).T

                cameras[count][str(count) + "_" + f"Camera{i}"] = newparam
                cameras[count + 1][str(count + 1) + "_" + f"Camera{i}"] = newparam

            count += 2
    return samples, datadict, datadict_3d, com3d_dict, cameras, exps


def make_pair(
    params,
    base_params,
    shared_args,
    shared_args_train,
    shared_args_valid,
    root="datasets/PAIR/PAIR-R24M-Dataset",
    viddir="videos_merged",
    train=True,
    merge_pair=False,
):
    # fix random seed
    np.random.seed(10241024)

    experiments_train = [
        "SR1_SR2",
        "SR1_SR3",
        "SR1_SR4",
        "SR2_SR3",
        "SR2_SR4",
        "SR3_SR4",
        "SR9_SR10",
    ]
    experiments_test = ["SR1_SR5", "SR3_SR5", "SR4_SR5"]

    metadata = pd.read_csv(os.path.join(root, "metadata.csv"))
    camnames = [f"Camera{i}" for i in range(1, 7)]
    all_exps = experiments_train + experiments_test
    num_experiments = len(all_exps)
    params["experiment"] = {}

    # load data
    samples = []
    datadict, datadict_3d, com3d_dict = {}, {}, {}
    cameras = {}
    partition = {}
    pairs = None

    (
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        exps_train,
    ) = _convert_pair_to_label3d(
        root,
        metadata,
        camnames,
        experiments_train,
        1000,
        0,
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        merge_pair,
    )

    partition["train_sampleIDs"] = sorted(samples)
    (
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        exps_valid,
    ) = _convert_pair_to_label3d(
        root,
        metadata,
        camnames,
        experiments_test,
        100,
        len(exps_train) * 2,
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        cameras,
        merge_pair,
    )
    partition["valid_sampleIDs"] = sorted(
        list(set(samples) - set(partition["train_sampleIDs"]))
    )
    samples = np.array(samples)
    exps = exps_train + exps_valid

    print("**DATASET SUMMARY**")
    print("Train: ")
    for i, exp in enumerate(exps_train):
        n_an1 = len([samp for samp in samples if int(samp.split("_")[0]) == 2 * i])
        n_an2 = len([samp for samp in samples if int(samp.split("_")[0]) == 2 * i + 1])
        print(f"{exp}: animal 1: {n_an1} + animal 2: {n_an2}")
    print("Validation: ")
    for i, exp in enumerate(exps_valid):
        n_an1 = len(
            [
                samp
                for samp in samples
                if int(samp.split("_")[0]) == 2 * len(exps_train) + 2 * i
            ]
        )
        n_an2 = len(
            [
                samp
                for samp in samples
                if int(samp.split("_")[0]) == 2 * len(exps_train) + 2 * i + 1
            ]
        )
        print(f"{exp}: animal 1: {n_an1} + animal 2: {n_an2}")
    print(
        "Train: Validation: {}: {}".format(
            len(partition["train_sampleIDs"]), len(partition["valid_sampleIDs"])
        )
    )

    vids = {}
    total_chunks = {}
    print("** Preparing video readers **")
    for e in tqdm(range(len(exps))):
        for name in camnames:
            vidroot = os.path.join(root, exps[e], viddir, name)
            video_files = os.listdir(vidroot)
            video_files = sorted(
                video_files, key=lambda x: int(x.split("-")[-1].split(".mp4")[0])
            )
            for i in range(2):
                total_chunks[str(2 * e + i) + "_" + name] = np.sort(
                    [int(x.split("-")[-1].split(".mp4")[0]) for x in video_files]
                )
                vids[str(2 * e + i) + "_" + name] = {}
                for file in video_files:
                    vids[str(2 * e + i) + "_" + name][
                        str(2 * e + i) + "_" + name + "/0.mp4"
                    ] = os.path.join(vidroot, file)

    new_camnames = {}
    for e in range(2 * len(exps)):
        new_camnames[e] = [str(e) + "_" + camname for camname in camnames]
    camnames = new_camnames

    # Dump the params into file for reproducibility
    save_params_pickle(params)
    # Setup additional variables for later use
    tifdirs = []  # Training from single images not yet supported in this demo
    base_params = {
        **base_params,
        "camnames": camnames,
        "vidreaders": vids,
        "chunks": total_chunks,
    }

    # Prepare datasets and dataloaders
    if params["use_npy"]:
        NPY_DIRNAMES = ["image_volumes", "grid_volumes", "targets"]

        TO_BE_EXAMINED = NPY_DIRNAMES
        npydir, missing_npydir = {}, {}

        for e, name in enumerate(exps):
            # for social, cannot use the same default npy volume dir for both animals
            for j in range(2):
                expid = 2 * e + j
                npy_folder = os.path.join(root, "npy_folder", name + f"an{j+1}")
                npydir[expid] = npy_folder

                # create missing npy directories
                if not os.path.exists(npydir[expid]):
                    missing_npydir[expid] = npydir[expid]
                    for dir in TO_BE_EXAMINED:
                        os.makedirs(os.path.join(npydir[expid], dir))
                else:
                    for dir in TO_BE_EXAMINED:
                        dirpath = os.path.join(npydir[expid], dir)
                        if (not os.path.exists(dirpath)) or (
                            len(os.listdir(dirpath)) == 0
                        ):
                            missing_npydir[expid] = npydir[expid]
                            os.makedirs(dirpath, exist_ok=True)

        missing_samples = [
            samp
            for samp in samples
            if int(samp.split("_")[0]) in list(missing_npydir.keys())
        ]

        # check any other missing npy samples
        for samp in list(set(samples) - set(missing_samples)):
            e, sampleID = int(samp.split("_")[0]), samp.split("_")[1]
            if not os.path.exists(
                os.path.join(npydir[e], "image_volumes", f"0_{sampleID}.npy")
            ):
                missing_samples.append(samp)
                missing_npydir[e] = npydir[e]

        missing_samples = np.array(sorted(missing_samples))

        train_generator, valid_generator = _make_data_npy(
            params,
            base_params,
            shared_args,
            shared_args_train,
            shared_args_valid,
            datadict,
            datadict_3d,
            com3d_dict,
            cameras,
            camnames,
            samples,
            partition,
            pairs,
            tifdirs,
            vids,
            rat7m=True,
            rat7m_npy=[npydir, missing_npydir, missing_samples],
        )
    else:
        train_generator, valid_generator = _make_data_mem(
            params,
            base_params,
            shared_args,
            shared_args_train,
            shared_args_valid,
            datadict,
            datadict_3d,
            com3d_dict,
            cameras,
            camnames,
            samples,
            partition,
            pairs,
            tifdirs,
            vids,
        )

    train_dataloader, valid_dataloader = serve_data_DANNCE.setup_dataloaders(
        train_generator, valid_generator, params
    )

    return train_dataloader, valid_dataloader, len(camnames[0])


def compute_segment_length_priors(params, pose3d, ref_segment_idx=3):
    notnan = ~np.isnan(pose3d.sum(-1).sum(-1))
    pose3d = pose3d[notnan]
    
    body_profile = params['loss'].get('body_profile', 'rat23')
    limbs = np.array(load_body_profile(body_profile)["limbs"])
    
    kpts_from = pose3d[:, limbs[:, 0]]
    kpts_to = pose3d[:, limbs[:, 1]]
    segment_lengths = np.linalg.norm(kpts_from - kpts_to, axis=-1) #[n_samples, n_segments]
    
    # compute relative ratios between segments to account for differences in body size
    # default idx = 3, earL-earR
    segment_lengths = segment_lengths / segment_lengths[:, ref_segment_idx][:, np.newaxis] # [n_samples, n_segments]
    segment_means = np.mean(segment_lengths, axis=0)
    segment_stds = np.std(segment_lengths, axis=0)
    
    priors = np.stack((segment_means, segment_stds), axis=-1)

    prior_savepath = os.path.join(
        os.path.abspath(params["dannce_train_dir"]),
        f"{body_profile}_priors.npy"
    )
    np.save(prior_savepath, priors)
    params['loss']['BoneLengthLoss']["priors"] = prior_savepath
    params['loss']['BoneLengthLoss']["relative_scale"] = True
    return prior_savepath