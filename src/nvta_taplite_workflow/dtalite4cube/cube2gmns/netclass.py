class Node:
    def __init__(self, node_id):
        self.node_id = node_id
        self.x_coord = None
        self.y_coord = None
        self.centroid = None
        self.zone_id = None
        self.geometry = None

        self.other_attrs = {}


class Link:
    def __init__(self, link_id):
        self.org_link_id = None
        self.link_id = link_id
        self.from_node = None
        self.to_node = None
        self.lanes = None
        self.length = 0
        self.dir_flag = 1
        self.geometry = None

        self.free_speed = None
        self.capacity = None
        self.link_type = 0
        self.vdf_code = 0

        self.other_attrs = {}


class Network:
    def __init__(self):
        self.node_dict = {}
        self.link_dict = {}

        self.max_node_id = 0
        self.max_link_id = 0

        self.original_coordinate_type = 'lonlat'

        self.node_other_attrs = []
        self.link_other_attrs = []

    @property
    def number_of_nodes(self):
        return len(self.node_dict)

    @property
    def number_of_links(self):
        return len(self.link_dict)
