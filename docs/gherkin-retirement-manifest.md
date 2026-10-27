# gherkin 退役豁免 manifest（SEE-1468 M5-a）

D 判定式（`git grep 'gqt gherkin' -- '.github/**' 'tests/**' 'features/**' 'docs/**' 'README.md'`）
的 checked-in 豁免清单：以下命中均为 **M5-a 别名期退役指引/横幅的自我指涉**
（ retired-alias 口径，M5-b 物理删除时随 runner 一并清零），非在役判据引用。

| # | 路径 | 性质 |
|---|---|---|
| 1 | README.md:45 | 工具表 retired-alias 行 |
| 2 | README.md:95 | 别名期横幅行为说明 |
| 3 | docs/contract.md:63 | 契约 4.1 节退役标注 |
| 4 | tests/unit/test_cli_steps_dir.py:3 | 横幅契约测试 docstring |
| 5 | tests/unit/test_cli.py:43 | 横幅契约测试 docstring |

D = 命中集(5) − 本 manifest(5) == 0。M5-b 物理删除轮随 runner 清零后本
manifest 一并删除。
