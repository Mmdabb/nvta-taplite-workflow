# DO NOT MODIFY KEYs!!!!

# time_period_dict = {
#     1: 'AM',
#     2: 'MD',
#     3: 'PM',
#     4: 'NT'
# }

time_period_dict = {
    'AM': 1,
    'MD': 2,
    'PM': 3,
    'NT': 4
}

node_file_name = "node.csv"
period_link_file_name = "link.csv"
link_file_template = "link_{time_period}.csv"
taz_jurisdiction_file_name = "TPBTAZ3722_TPBMod_JUR.csv"

bpr_vdf_params = ("alpha", "beta", "plf")
qvdf_params = ("alpha", "beta", "qdf", "plf", "cp", "cd", "n", "s")

qvdf_source_field_template = "QVDF_{qvdf_param}{period_sequence}"
bpr_vdf_source_field_template = "VDF_{vdf_param}{period_sequence}"
new_vdf_output_field_template = "vdf_{vdf_param}"

new_allowed_uses_field = "allowed_use"
new_fftt_field = "vdf_fftt"
new_alpha_field = "vdf_alpha"
new_beta_field = "vdf_beta"
new_plf_field = "vdf_plf"
new_toll_field = "toll"
supported_mode_toll_types = ("sov", "hov2", "hov3", "com", "trk", "apv")
new_ref_volume_field = "ref_volume"
new_ref_cost_field = "ref_cost"
new_vdf_free_speed_mph_field = "vdf_free_speed_mph"
new_vdf_length_mi_field = "vdf_length_mi"
new_vdf_type_field = "vdf_type"
crs_field = "crs"

length_mile_internal_field = "length_in_mile"
district_id_field = "district_id"
pair_field = "pair"

taz_field = "TAZ"
jurisdiction_name_field = "NAME"
generated_jurisdiction_field = "JUR_NAME"

its_field = "ITS"
intersection_field = "INTERSECTI"

capacity_field = "capacity"
node_id_field = "node_id"
link_id_field = "link_id"
from_node_id_field = "from_node_id"
to_node_id_field = "to_node_id"

# Field name mapping for cube node structure
cube_node_mapping = {
    'from_node_field': 'A',
    'to_node_field': 'B',
    'geometry_field': 'geometry'
}

# Field name mapping for cube base link structure
cube_base_link_mapping = {
    'link_id_field': 'ID',
    'from_node_field': 'A',
    'to_node_field': 'B',
    'geometry_field': 'geometry',
    'distance_field': 'DISTANCE',
    # 'lane_field': '',
    'area_type_field': 'ATYPE',
    'facility_type_field': 'FTYPE',
    'capacity_class': 'CAPCLASS',
    'speed_class': 'SPDCLASS'
}

cube_link_dependent_mapping = {
    'lane_field': '{time_period}LANE',
    'limit_field': '{time_period}LIMIT',
    'toll_field': '{time_period}TOLL'
}

# DTALite fields mapping
dtalite_node_mapping = {
    'node_id': node_id_field,
    'zone_id': 'zone_id',
    'x_coord': 'x_coord',
    'y_coord': 'y_coord',
}

dtalite_base_link_mapping = {
    'link_id': link_id_field,
    'from_node_id': from_node_id_field,
    'to_node_id': to_node_id_field,
    'dir_flag': 'dir_flag',
    'length': 'length',
    'lanes': 'lanes',
    'capacity': capacity_field,
    'free_speed': 'free_speed',
    'toll': new_toll_field,
    'link_type': 'link_type',
    'allowed_use': new_allowed_uses_field,
    'vdf_alpha': new_alpha_field,
    'vdf_beta': new_beta_field,
    'vdf_plf': new_plf_field,
    'ref_volume': new_ref_volume_field,
    'ref_cost': new_ref_cost_field,
    'vdf_free_speed_mph': new_vdf_free_speed_mph_field,
    'vdf_length_mi': new_vdf_length_mi_field,
    'vdf_fftt': new_fftt_field,
    'crs': crs_field,
    'geometry': 'geometry',
}
dtalite_additional_link_mapping = {
    'vdf_parameter': new_vdf_output_field_template
}
