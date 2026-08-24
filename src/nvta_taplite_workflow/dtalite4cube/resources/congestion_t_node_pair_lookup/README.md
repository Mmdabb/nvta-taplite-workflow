# Node-pair boundary lookup

These files are a one-time export from the completed NVTA link files. Each AM, MD, and PM
file is a sorted NumPy structured array keyed by `(from_node_id, to_node_id)`.

## Why `.npy`

- `numpy.load(path, mmap_mode="r")` opens the lookup without loading the full array into RAM.
- Sorted packed 64-bit keys support vectorized `numpy.searchsorted` matching.
- The format preserves typed node ids and float boundary values without CSV parsing.
- No dependency is required beyond NumPy.

## Fields

- `packed_key`: `(uint64(from_node_id) << 32) | uint64(to_node_id)`
- `from_node_id`, `to_node_id`: original pair, stored as unsigned 32-bit integers
- `t0_hour`, `t2_hour`, `t3_hour`: completed boundaries stored as float32 hours

No class estimate, source label, uncertainty field, or other reference column is included.

The float32 export changes the source values by far less than one second; the exact maximum
round-trip difference is recorded in `metadata.json`.

## Load and map

```python
import pandas as pd
from load_node_pair_boundaries import lookup_period

network = pd.read_csv("my_network.csv")
values, found = lookup_period(
    "AM",
    network["from_node_id"].to_numpy(),
    network["to_node_id"].to_numpy(),
    directory="path/to/node_pair_lookup",
)
network[["t0_hour", "t2_hour", "t3_hour"]] = values
```

`found` identifies network pairs that exist in the lookup. The files are sorted and contain
one unique row per directed node pair.
