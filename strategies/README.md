# 策略插件接口

这里放自定义策略插件。配置里把对应策略的 `mode` 改成 `plugin:文件名` 即可加载。

例如：

```json
{
  "selection_strategy": {
    "mode": "plugin:sample_momentum",
    "min_on_sale_count": 5
  },
  "strategy_plugins": {
    "directories": ["strategies"]
  }
}
```

如果一个策略包同时实现选品、定价、数量，可以三个 `mode` 都指向同一个文件：

```json
{
  "selection_strategy": {
    "mode": "plugin:cs2_2026_low_price_bid"
  },
  "bid_strategy": {
    "mode": "plugin:cs2_2026_low_price_bid"
  },
  "quantity_strategy": {
    "mode": "plugin:cs2_2026_low_price_bid"
  }
}
```

选品插件必须提供：

```python
def select_candidates(candidates, config):
    return candidates
```

定价插件必须提供：

```python
def calculate_bid_price(client, template, config, existing_order):
    return price, reason
```

数量插件必须提供：

```python
def allocate_quantities(prices, config):
    return quantities
```

如果要让桌面端自动显示这个策略，需要在模块里声明 `DESKTOP_PROFILE`：

```python
STRATEGY_ID = "my_strategy"
STRATEGY_NAME = "我的策略"

DESKTOP_PROFILE = {
    "label": STRATEGY_NAME,
    "selection": "plugin:my_strategy",
    "bid": "plugin:my_strategy",
    "quantity": "plugin:my_strategy",
    "create_channel": "emulator_queue",
    "allow_create": True,
    "budget": 1000,
    "max_quantity_per_item": 5,
    "max_quantity_hard_limit": 7,
    "active_order_exposure_multiplier": 2,
    "top_n": 10,
    "note": "显示在桌面端策略说明里的文字。",
}
```

桌面端启动时会扫描 `strategy_plugins.directories`，读取每个模块的 `DESKTOP_PROFILE` 和 `DEFAULT_CONFIG`。点策略下拉框里的 `应用` 后，App 会把这个 Profile 对应的选品、定价、数量模式和默认参数写入运行时配置。

`candidates` 是 `StickerTemplate` 列表。每个对象常用字段：

- `template_id`
- `name`
- `hash_name`
- `lowest_sell_price`
- `on_sale_count`
- `raw`

可以给 `item.raw["_selection_score"]` 和 `item.raw["_selection_reason"]` 写入分数和原因，桌面端计划表会显示原因。

公共工具优先从 `youpin_strategy_sdk.py` 引入，不要直接依赖主执行器。

当前项目基准策略包：

- `cs2_2026_low_price_bid.py`：26低价贴纸求购策略，作为独立策略模块接入桌面端；封装最便宜 10 个品类、顺位追价、单品 5-7 张和 24h 成交上限逻辑。
