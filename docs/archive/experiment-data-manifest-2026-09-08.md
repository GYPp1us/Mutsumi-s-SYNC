# 实验与生成数据归档清单

**归档日期**：2026-09-08（UTC+8）  
**原则**：只移动已确认的本地生成物，不删除生产日志或媒体文件。目录位于 `data/` 下，受 `.gitignore` 忽略；文件仍可恢复到原位置。

## 移动记录

| 原位置 | 归档位置 | 内容 | 归档前数量/大小 |
| --- | --- | --- | ---: |
| `data/prototype/` | `data/archive/2026-09-08/prototype/` | 两个演示 SQLite、prototype `.log` 和 `.ndjson` | 4 个文件，521,403 bytes |
| `data/design-options/` | `data/archive/2026-09-08/design-options/` | 报告检查/渲染脚本及 4 张 QA 截图 | 6 个文件，449,401 bytes |
| `data/test-debug-context.db` | `data/archive/2026-09-08/test-debug/test-debug-context.db` | 调试用 SQLite | 1 个文件，176,128 bytes |
| `experiments/context_v2/`、`experiments/context_v3/` 的 `__pycache__/` | 删除缓存目录 | Python 字节码缓存，不是实验结果 | 16 个缓存文件，137,079 bytes |

移动前已记录每个生成文件的 SHA-256；下表列出可复核的值。若某次执行中目录不存在，应在报告中写明“无文件可移动”，不要创建空的伪结果。

## SHA-256

| 归档文件（相对仓库根） | SHA-256 |
| --- | --- |
| `data/archive/2026-09-08/prototype/dev-demo.sqlite3` | `54F5FAD519ACC6948D3EFC1ED98F49A586AD358999203726FC646445B45380CD` |
| `data/archive/2026-09-08/prototype/final-demo.sqlite3` | `55C24ED3A363DBB4D16A1B26030AE5F75AA867474B9443A72A5F75E2739A8C41` |
| `data/archive/2026-09-08/prototype/logs/prototype.log` | `DDA9A8963CFC2764A7B752E289B3E2F8B2C0B619F7DB26A3063F259482708B2E` |
| `data/archive/2026-09-08/prototype/logs/prototype.ndjson` | `877DD994A6B474A8ADE1AD95D23537F53CF4D45B5FEF1B0FBACDF59545E630D1` |
| `data/archive/2026-09-08/design-options/check-report.cjs` | `C8E85777A0FB4BF729A18A4CA8F3FAF681FB9384F044DF6D116916B2707C461E` |
| `data/archive/2026-09-08/design-options/intent-handoff-desktop.png` | `B3BE36A86853BD8FA688CE707574881468679C1E3B0C484063E92E046AEAC299` |
| `data/archive/2026-09-08/design-options/intent-handoff-mobile.png` | `08440536EF29111BA5FE60BCDF7BF0299771B54EE6E05160922D2647283414A5` |
| `data/archive/2026-09-08/design-options/render-report.cjs` | `02006C9A97F41BE673A6AD4BAC2F1A0287B6CF4ED50B1D66F4F2B1EF2CC0C598` |
| `data/archive/2026-09-08/design-options/report-desktop.png` | `70363796BE26BB26CE2DEB69719D640E7E61FE0B1265B3F57F7C8EF853F686D5` |
| `data/archive/2026-09-08/design-options/report-mobile.png` | `CC4801C56989A79D058FC5DCA55275E8545B9633FD11D12EA874BECEF82A6E60` |
| `data/archive/2026-09-08/test-debug/test-debug-context.db` | `5DAB45A3716FD3E966BCCFD176C956432727B4E0D2A2778770DFE9C7EA7723D4` |

## 不在清单中的保留项

- `data/logs/mutsumi.log`；
- `data/media/` 及其图片；
- 任何生产数据库、NDJSON 日志或未确认用途的 SQLite；
- `.reasonix/`、`.pytest_cache/`、`.venv/` 等本机缓存。

归档数据只为回放和核对服务。新的编排实验应使用日期和策略目录，并在每次运行记录模型、提示版本、场景版本、原始响应哈希和人工评分。
