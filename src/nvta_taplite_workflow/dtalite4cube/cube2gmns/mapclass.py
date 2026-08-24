class Mapping:
    """
    Class for field mapping.

    Attributes:
        from_node_field (str): Field name for the from node.
        to_node_field (str): Field name for the to node.
        geometry_field (str): Field name for the geometry.
    """

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            if value:
                setattr(self, key, value)
            else:
                print(f"Ignoring empty value for attribute '{key}'.")


class DependentMapping(Mapping):
    def get_field(self, key, dependent_var):
        dependent_field = getattr(self, key, None)
        if dependent_field:
            variable = dependent_field.split('{')[1].split('}')[0]
            return dependent_field.format(**{variable: dependent_var})
            # return dependent_field.format(dependent_var=dependent_var)
        return None
