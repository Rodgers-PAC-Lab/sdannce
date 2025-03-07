import os
from logging import RootLogger

import scipy.io as sio

ROOT = os.path.abspath(os.path.dirname(__file__))
_BODY_PROFILES = [
    file.split(".mat")[0] for file in os.listdir(ROOT) if file.endswith(".mat")
]


_BODY_CONNECTIVITY = {
    "rat23": [
        [0, 1],
        [0, 2],
        [0, 3],
        [1, 2],
        [3, 4],
        [3, 7],
        [3, 11],
        [4, 5],
        [5, 6],
        [5, 15],
        [5, 19],
        [7, 8],
        [8, 9],
        [9, 10],
        [11, 12],
        [12, 13],
        [13, 14],
        [15, 16],
        [16, 17],
        [17, 18],
        [19, 20],
        [20, 21],
        [21, 22],
    ],
    "rat16": [
        [0, 1],
        [1, 2],
        [0, 2],
        [0, 3],
        [3, 4],
        [4, 5],
        [5, 6],
        [6, 7],
        [8, 9],
        [3, 9],
        [10, 11],
        [11, 3],
        [12, 13],
        [13, 5],
        [14, 15],
        [15, 5],
    ],
    "rat7m": [
        [0, 1],
        [0, 2],
        [1, 2],
        [2, 3],
        [3, 4],
        [3, 6],
        [3, 12],
        [3, 13],
        [4, 5],
        [5, 7],
        [5, 8],
        [5, 9],
        [6, 7],
        [8, 17],
        [9, 16],
        [17, 18],
        [16, 19],
        [10, 11],
        [12, 10],
        [13, 14],
        [14, 15],
    ],
    "mouse22": [
        [0, 1],
        [1, 2],
        [0, 2],
        [0, 3],
        [1, 3],
        [2, 3],
        [3, 4],
        [4, 5],
        [5, 6],
        [6, 7],
        [8, 9],
        [9, 10],
        [10, 11],
        [11, 3],
        [12, 13],
        [13, 14],
        [14, 15],
        [15, 3],
        [16, 17],
        [17, 18],
        [18, 4],
        [19, 20],
        [20, 21],
        [21, 4],
    ],
    "mouse14": [
        [0, 1],
        [0, 2],
        [1, 2],
        [0, 3],
        [3, 4],
        [4, 5],
        [3, 6],
        [6, 7],
        [3, 8],
        [8, 9],
        [4, 10],
        [10, 11],
        [4, 12],
        [12, 13],
    ],
}

_JOINT_NAMES = {
    "rat23": [
        "Snout",
        "EarL",
        "EarR",
        "SpineF",
        "SpineM",
        "SpineL",
        "TailBase",
        "ShoulderL",
        "ElbowL",
        "WristL",
        "HandL",
        "ShoulderR",
        "ElbowR",
        "WristR",
        "HandR",
        "HipL",
        "KneeL",
        "AnkleL",
        "FootL",
        "HipR",
        "KneeR",
        "AnkleR",
        "FootR",
    ],
    "rat16": [
        "EarL",
        "EarR",
        "Snout",
        "SpineF",
        "SpineM",
        "Tail(base)",
        "Tail(mid)",
        "Tail(end)",
        "ForepawL",
        "ForelimbL",
        "ForepawR",
        "ForelimbR",
        "HindpawL",
        "HindlimbL",
        "HindpawR",
        "HindlimbR",
    ],
    "rat7m": [
        "HeadF",
        "HeadB",
        "HeadL",
        "SpineF",
        "SpineM",
        "SpineL",
        "Offset1",
        "Offset2",
        "HipL",
        "HipR",
        "ElbowL",
        "ArmL",
        "ShoulderL",
        "ShoulderR",
        "ElbowR",
        "ArmR",
        "KneeR",
        "KneeL",
        "ShinL",
        "ShinR",
    ],
    "mouse22": [
        "EarL",
        "EarR",
        "Snout",
        "SpineF",
        "SpineM",
        "Tail(base)",
        "Tail(mid)",
        "Tail(end)",
        "ForepawL",
        "WristL",
        "ElbowL",
        "ShoulderL",
        "ForepawR",
        "WristR",
        "ElbowR",
        "ShoulderR",
        "HindpawL",
        "AnkleL",
        "KneeL",
        "HindpawR",
        "AnkleR",
        "KneeR",
    ],
    "mouse14": [
        "Snout",
        "EarL",
        "EarR",
        "SpineF",
        "SpineM",
        "Tail(base)",
        "ForShdL",
        "ForepawL",
        "ForeShdR",
        "ForepawR",
        "HindShdL",
        "HindpawL",
        "HindShdR",
        "HindpawR",
    ],
}

SYMMETRY = {
    "mouse22": [
        [(1, 2), (0, 2)],
        [(0, 3), (1, 3)],
        [(9, 10), (13, 14)],
        [(10, 11), (14, 15)],
        [(11, 3), (15, 3)],
        [(12, 13), (8, 9)],
        [(16, 17), (19, 20)],
        [(17, 18), (20, 21)],
        [(18, 4), (21, 4)],
    ]
}


def load_body_profile(name):
    assert (name in _BODY_CONNECTIVITY) and (
        name in _JOINT_NAMES
    ), f"{name} not a valid skeleton profile"
    return {"limbs": _BODY_CONNECTIVITY[name], "joint_names": _JOINT_NAMES[name]}
