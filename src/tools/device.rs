use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_devices",
            "get_devices",
            "List a track's device chain: each device's index, name, class, type \
             (instrument, audio effect, midi effect), whether it is on, and whether it is a \
             rack, with its chains listed so you can address devices nested inside it.",
        )
        .read_only()
        .track_ref(),

        ToolDef::new(
            "get_device_parameters",
            "get_device_parameters",
            "Read every parameter of a device, with the information needed to actually set it: \
             name, current value, min and max, the value formatted the way Live displays it \
             (so you see \"2.5 kHz\" rather than 0.63), whether the parameter is quantized, \
             and for quantized parameters the list of named settings in order, which is how \
             you learn that a filter type of 2 means Bandpass. Read this before \
             `set_device_parameter`.",
        )
        .read_only()
        .device_ref(),

        ToolDef::new(
            "set_device_parameter",
            "set_device_parameter",
            "Set one device parameter. Identify it by index or by name. Name is safer, since \
             indices shift between device versions. Values are in the parameter's own units \
             and are clamped to its range; for a quantized parameter, pass the position of the \
             setting you want.",
        )
        .device_ref()
        .opt_int("parameter_index", "Index into the device's parameters.")
        .opt_text("parameter_name", "Parameter name as reported by `get_device_parameters`. Case-insensitive.")
        .num("value", "New value, in the parameter's own units."),

        ToolDef::new(
            "set_device_parameters",
            "set_device_parameters",
            "Set several parameters of one device at once, dialling in a whole patch in a \
             single pass rather than one round trip per knob.",
        )
        .device_ref()
        .opt_any(
            "values",
            "Array of `{\"name\": \"Filter Freq\", \"value\": 800}` or \
             `{\"index\": 12, \"value\": 800}` objects.",
        ),

        ToolDef::new(
            "set_device_enabled",
            "set_device_enabled",
            "Switch a device on or off, as its power button does.",
        )
        .device_ref()
        .boolean("enabled", "Whether the device is active."),

        ToolDef::new("delete_device", "delete_device", "Remove a device from a track's chain.")
            .destructive()
            .device_ref(),

        ToolDef::new(
            "get_rack_chains",
            "get_rack_chains",
            "Inspect an Instrument, Drum, Audio Effect or MIDI Effect Rack: its macro controls \
             and their names, and each chain with its mixer state, key and velocity zones, and \
             devices. Drum racks also report which note each pad sits on.",
        )
        .read_only()
        .device_ref(),

        ToolDef::new(
            "set_chain_mixer",
            "set_chain_mixer",
            "Change a rack chain's own mixer: the balance between layers of an Instrument \
             Rack, or the level of one drum pad. Omit anything you do not want to touch.",
        )
        .device_ref()
        .int("chain_index", "0-based index into the rack's chains.")
        .opt_text("name", "Chain name.")
        .opt_int("color", "Colour as 0xRRGGBB.")
        .opt_num("volume", "Chain fader from 0.0 to 1.0, where 0.85 is unity.")
        .opt_num("panning", "-1.0 hard left to 1.0 hard right.")
        .opt_bool("mute", "Mute the chain.")
        .opt_bool("solo", "Solo the chain."),
    ]
}
