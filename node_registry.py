from dataclasses import dataclass, field


@dataclass
class NodeRegistry:
    """
    Shared registries that map between DearPyGui item IDs and logical UUIDs.

    A single instance is created by NodeManager and passed by reference to
    PropertyPanel so that both components always see the same up-to-date
    mappings without duplicating state.
    """

    # DPG node-item id  ->  uuid string
    dpg_to_uuid: dict = field(default_factory=dict)

    # uuid string  ->  DPG node-item id
    uuid_to_dpg: dict = field(default_factory=dict)

    # DPG attribute id  ->  (uuid, attribute_name)
    input_attr_registry: dict = field(default_factory=dict)

    # DPG attribute id  ->  (uuid, attribute_name)
    output_attr_registry: dict = field(default_factory=dict)

    # DPG link id  ->  (src_uuid, src_attr, dst_uuid, dst_attr)
    link_registry: dict = field(default_factory=dict)

    def clear(self):
        """Clear all five registries (called by NodeManager.clear_all)."""
        self.dpg_to_uuid.clear()
        self.uuid_to_dpg.clear()
        self.input_attr_registry.clear()
        self.output_attr_registry.clear()
        self.link_registry.clear()
