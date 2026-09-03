# CTP native library variants

| variant | binary marker | use with `--env` |
| --- | --- | --- |
| `simnow` | `v6.7.7_MacOS_20240716` | `first`, `7x24` |
| `guangfa` | `v6.7.7_MacOS_CP_20240716` | `guangfa` |

`live_grid.ctp_native.activate_ctp_native_libs` copies the selected pair into the
active Mac frameworks (or Linux/Windows shared libs) **before** importing
`vnpy_ctp` native extensions. Do not mix variants inside one process.
