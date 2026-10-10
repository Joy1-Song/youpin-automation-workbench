from decimal import Decimal


def _dec(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", ""))
    except Exception:
        return Decimal(default)


def select_candidates(candidates, config):
    """示例：偏向求购热度高、卖盘较少的标的。

    这是一个模板策略，可以复制成自己的文件后修改。主程序会传入搜索到的候选贴纸列表，
    返回值顺序就是后续求购计划的优先级。
    """
    selection = config.get("selection_strategy", {})
    min_on_sale = int(selection.get("min_on_sale_count", 0))
    filtered = [
        item
        for item in candidates
        if item.lowest_sell_price is not None
        and item.lowest_sell_price > 0
        and (item.on_sale_count or 0) >= min_on_sale
    ]

    scored = []
    for item in filtered:
        price = _dec(item.lowest_sell_price)
        supply = Decimal(item.on_sale_count or 0)
        demand_text = item.raw.get("templatePurchaseCountText") or item.raw.get("TemplatePurchaseCountText") or "0"
        demand_digits = "".join(ch for ch in str(demand_text) if ch.isdigit())
        demand = Decimal(demand_digits or "0")
        demand_supply = demand / max(supply, Decimal("1"))
        affordability = Decimal("1") / max(price, Decimal("0.1"))
        score = demand_supply * Decimal("70") + affordability * Decimal("30")
        item.raw["_selection_score"] = float(score)
        item.raw["_selection_reason"] = (
            f"插件动量：score={score.quantize(Decimal('0.01'))} "
            f"demand={int(demand)} on_sale={int(supply)}"
        )
        scored.append(item)

    return sorted(
        scored,
        key=lambda item: (
            -Decimal(str(item.raw.get("_selection_score", 0))),
            item.lowest_sell_price or Decimal("999999"),
            item.name,
        ),
    )
