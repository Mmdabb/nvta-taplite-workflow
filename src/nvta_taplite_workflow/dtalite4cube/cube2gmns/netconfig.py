# Define time period dictionary

# mapping facility type to alpha value for BPR function
alpha_dict = {
    0: 0.1,
    1: 0.87,
    2: 0.96,
    3: 0.96,
    4: 0.96,
    5: 0.87,
    6: 0.96
}
# mapping facility type to beta value for BPR function
beta_dict = {
    0: 2,
    1: 5,
    2: 2.3,
    3: 2.3,
    4: 2.3,
    5: 5,
    6: 2.3
}
# allowed use (or allowed agents) are coded as 0 to 9 in the field {time_period}LIMIT
allowed_uses_dict = {
    0: 'sov;hov2;hov3;trk;apv;com',
    1: 'sov;hov2;hov3;trk;apv;com',
    2: 'hov2;hov3',
    3: 'hov3',
    4: 'sov;hov2;hov3;com;apv',
    5: 'apv',
    6: '',
    7: '',
    8: '',
    9: 'closed'
}
# mapping SPDCLASS to speed in mph
speed_class_dict = {
    0: 17, 1: 17, 2: 17, 3: 23, 4: 29, 5: 35, 6: 40, 11: 63, 12: 63, 13: 69, 14: 69, 15: 75,
    16: 75, 21: 40, 22: 40, 23: 52, 24: 52, 25: 58, 26: 58, 31: 40, 32: 40, 33: 46, 34: 46,
    35: 46, 36: 52, 41: 35, 42: 35, 43: 35, 44: 40, 45: 40, 46: 40, 51: 52, 52: 52, 53: 58,
    54: 58, 55: 58, 56: 63, 61: 23, 62: 23, 63: 35, 64: 35, 65: 40, 66: 58
}
# mapping CAPCLASS to capacity
capacity_class_dict = {
    0: 3150, 1: 3150, 2: 3150, 3: 3150, 4: 3150, 5: 3150, 6: 3150, 11: 1900, 12: 1900,
    13: 2000, 14: 2000, 15: 2000, 16: 2000, 21: 600, 22: 800, 23: 960, 24: 960,
    25: 1100, 26: 1100, 31: 500, 32: 600, 33: 700, 34: 840, 35: 900, 36: 900, 41: 500,
    42: 500, 43: 600, 44: 800, 45: 800, 46: 800, 51: 1100, 52: 1200, 53: 1200, 54: 1400,
    55: 1600, 56: 1600, 61: 1000, 62: 1000, 63: 1000, 64: 1000, 65: 2000, 66: 2000
}

phf_dict = {
    'AM': 2.39776486,
    'MD': 5.649424854,
    'PM': 3.401127052,
    'NT': 6.66626961
}
