# 真实 SillyTavern 卡语料（N9 的收尾项）

把从 SillyTavern 里**导出为 JSON** 的人物卡（或独立世界书 JSON）放在这个目录，
`pytest` 与体检脚本会自动带上它们——不需要改任何代码。

## 为什么要有这个目录（现状：**环境受限未验**）

> 作者已决定：这一条**记为未验并收尾**，不为此挂起 N9（2026-10）。
> 通道留着——卡到了就会被测试自动带上；在那之前 pytest 里那条 `skip` 就是如实状态。

`docs/plan-dsh-tavern-parity.md` 期 4 的纪律原话：

> E-1 必须**由一份真实酒馆人物卡驱动**（真卡语料进 `samples/`），
> **不要**先按映射表把字段铺满再等用例——否则会复现同一类"校验通过但从未运行"的死代码。

本仓库目前**没有**真实卡（作者的环境无网络，手里也没有现成的卡）。已有的往返测试由
本项目 8 个世界包作者真实写下的 44 条 lore 驱动，但实测它们**只用到 `keys` 与 `text`**：

| 映射行 | 真实语料验证情况 |
|---|---|
| `keys` / `content` | ✅ 44 条全部覆盖 |
| `secondary_keys` | ⚠️ 零用量（只有构造数据测过） |
| `constant` | ⚠️ 零用量（只有构造数据测过） |
| `/re/` 正则键 | ⚠️ 零用量（只有构造数据测过） |

放一张真实卡进来，这张表就能补上。

## 怎么用

```bash
# 1) 体检：这份卡用到哪些映射字段、会丢掉什么（只读，不改任何东西）
python scripts/st_card_probe.py samples/st_cards/<your-card>.json

# 2) 让往返测试带上它（放进来就自动生效；没有文件时那条测试会 skip 并说明原因）
uv run pytest tests/test_st_convert.py -q

# 3) 真的从这张卡生成一个世界包（离线试跑，不花钱）
uv run python scripts/import_story.py samples/st_cards/<your-card>.json \
    --name my_card_pack --format sillytavern --offline
```

## 注意

- **只支持 JSON**。PNG 卡请在 SillyTavern 里右键选「导出为 JSON」——PNG 元数据有
  `tEXt/zTXt/iTXt` 三种块的写法差异，本项目只做能验证的路径（详见
  `game_agent/st_convert.py` 的模块说明）。
- 卡片内容会进 git 历史。**别人的卡请先确认它的授权**；不确定就自己在酒馆里做一张
  测试卡放进来（一样能验证映射，只是不覆盖真实世界的用词习惯）。
