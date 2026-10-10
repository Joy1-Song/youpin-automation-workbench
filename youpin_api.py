from __future__ import annotations

import json
import random
import re
import string
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


class YouPinError(RuntimeError):
    pass


def random_string(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def random_hex(length: int) -> str:
    alphabet = "0123456789abcdef"
    return "".join(random.choice(alphabet) for _ in range(length))


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return Decimal(match.group(0))
        except (InvalidOperation, ValueError):
            return None


def money(value: Decimal, places: int = 2, rounding: str = ROUND_HALF_UP) -> Decimal:
    quantum = Decimal("1").scaleb(-places)
    return value.quantize(quantum, rounding=rounding)


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().replace("\u2019", "'").split())


def pick(source: dict[str, Any], keys: list[str]) -> Any:
    lowered = {key.lower(): key for key in source}
    for key in keys:
        actual = lowered.get(key.lower())
        if actual is not None:
            return source[actual]
    return None


def parse_count_text(value: Any) -> int:
    if value is None:
        return 0
    text = str(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def deep_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(k.lower() in {"templateid", "id", "commodityname"} for k in value):
            records.append(value)
        for child in value.values():
            records.extend(deep_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(deep_records(child))
    return records


def response_code(payload: dict[str, Any]) -> Any:
    return payload.get("code", payload.get("Code"))


def response_msg(payload: dict[str, Any]) -> str:
    return str(payload.get("msg", payload.get("Msg", "")))


@dataclass(frozen=True)
class StickerTemplate:
    template_id: int
    name: str
    hash_name: str
    lowest_sell_price: Decimal | None
    on_sale_count: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ExistingPurchaseOrder:
    order_no: str
    template_id: int
    commodity_name: str
    purchase_price: Decimal | None
    max_purchase_price: Decimal | None
    purchase_num: int | None
    supply_quantity: int
    rank: int | None
    raw: dict[str, Any]

    @property
    def spend(self) -> Decimal:
        if self.purchase_price is None or self.purchase_num is None:
            return Decimal("0")
        return self.purchase_price * Decimal(self.purchase_num)

    @property
    def fill_text(self) -> str:
        total = "?" if self.purchase_num is None else str(self.purchase_num)
        return f"{self.supply_quantity}/{total}"


@dataclass(frozen=True)
class InventoryItem:
    asset_id: str
    template_id: int | None
    name: str
    market_hash_name: str
    asset_status: int | None
    tradable: bool | None
    cooldown_desc: str
    buy_price: Decimal | None
    reference_price: Decimal | None
    price_desc: str
    raw: dict[str, Any]

    @property
    def commodity_id(self) -> str:
        value = self.raw.get("CommodityId", self.raw.get("commodityId", ""))
        return str(value or "").strip()

    @property
    def is_on_sale(self) -> bool:
        return self.asset_status == 3 and bool(self.commodity_id)

    @property
    def display_cooldown(self) -> str:
        return self.cooldown_desc or "可交易"

    @property
    def status_text(self) -> str:
        if "不可交易" in self.cooldown_desc:
            return "不可交易"
        if self.tradable is False:
            if self.cooldown_desc:
                return "CD中"
            return "不可交易"
        if self.cooldown_desc:
            return "CD中"
        return "可交易"


@dataclass(frozen=True)
class MarketSnapshot:
    template_id: int | None
    name: str
    market_hash_name: str
    lowest_sell_price: Decimal | None
    highest_purchase_price: Decimal | None
    on_sale_count: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ExitAction:
    action: str
    item: InventoryItem
    target_price: Decimal
    action_price: Decimal | None
    snapshot: MarketSnapshot | None
    rule: dict[str, Any]
    reason: str

    @property
    def is_actionable(self) -> bool:
        if self.action == "delist_sale":
            return self.item.is_on_sale
        if self.action == "relist_sale":
            return self.item.is_on_sale and self.action_price is not None
        return (
            self.action in {"list_sale", "drop_to_bid"}
            and self.action_price is not None
            and self.item.status_text == "可交易"
        )


@dataclass(frozen=True)
class PlanAction:
    action: str
    template: StickerTemplate
    bid_price: Decimal
    purchase_num: int
    reference_price: Decimal | None
    existing_order: ExistingPurchaseOrder | None
    reason: str

    @property
    def spend(self) -> Decimal:
        return self.bid_price * Decimal(self.purchase_num)


@dataclass(frozen=True)
class CleanupAction:
    order: ExistingPurchaseOrder
    reason: str

    @property
    def spend(self) -> Decimal:
        return self.order.spend


@dataclass(frozen=True)
class CyclePlan:
    candidates: list[StickerTemplate]
    actions: list[PlanAction]
    cleanup_actions: list[CleanupAction]
    existing_orders: dict[int, ExistingPurchaseOrder]
    state: dict[str, Any]

    @property
    def active_spend(self) -> Decimal:
        return sum((action.spend for action in self.actions), Decimal("0"))

    @property
    def new_exposure(self) -> Decimal:
        exposure = Decimal("0")
        for action in self.actions:
            if action.action == "keep":
                continue
            if action.existing_order is None:
                exposure += action.spend
            else:
                exposure += max(action.spend - action.existing_order.spend, Decimal("0"))
        return exposure


@dataclass(frozen=True)
class PurchaseRiskSnapshot:
    enabled: bool
    limit: Decimal
    lookback_hours: int
    recent_success_spend: Decimal
    recent_success_count: int
    recent_purchases: list[dict[str, Any]]
    active_order_spend: Decimal
    active_order_spend_after_plan: Decimal
    active_order_exposure_limit: Decimal
    active_order_exposure_multiplier: Decimal
    include_active_order_exposure: bool
    checked_at: str
    window_mode: str = "rolling"
    window_start: str = ""

    @property
    def remaining_success_budget(self) -> Decimal:
        return max(self.limit - self.recent_success_spend, Decimal("0"))

    @property
    def projected_total_risk(self) -> Decimal:
        if not self.include_active_order_exposure:
            return self.recent_success_spend
        return self.recent_success_spend + self.active_order_spend_after_plan

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rolling_success_limit": str(self.limit),
            "lookback_hours": self.lookback_hours,
            "recent_success_spend": str(self.recent_success_spend),
            "recent_success_count": self.recent_success_count,
            "recent_purchases": self.recent_purchases,
            "active_order_spend": str(self.active_order_spend),
            "active_order_spend_after_plan": str(self.active_order_spend_after_plan),
            "active_order_exposure_limit": str(self.active_order_exposure_limit),
            "active_order_exposure_multiplier": str(self.active_order_exposure_multiplier),
            "include_active_order_exposure": self.include_active_order_exposure,
            "remaining_success_budget": str(self.remaining_success_budget),
            "projected_total_risk": str(self.projected_total_risk),
            "checked_at": self.checked_at,
            "window_mode": self.window_mode,
            "window_start": self.window_start,
        }


class YouPinClient:
    def __init__(
        self,
        api_base: str,
        token: str = "",
        device_token: str = "",
        cookie: str = "",
        uk: str = "",
        device_uk: str = "",
        device_id: str = "",
        timeout: int = 20,
    ):
        self.api_base = api_base.rstrip("/") + "/"
        self.token = token.strip()
        self.uk = uk.strip()
        self.device_uk = (device_uk or device_token).strip()
        self.device_id = device_id.strip()
        self.cookie = cookie.strip()
        self.timeout = timeout

    def headers(self) -> dict[str, str]:
        trace_id = random_hex(32)
        span_id = random_hex(16)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.youpin898.com",
            "Referer": "https://www.youpin898.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
            ),
            "AppVersion": "5.26.0",
            "App-Version": "5.26.0",
            "secret-v": "h5_v1",
            "appType": "1",
            "platform": "pc",
            "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "b3": f"{trace_id}-{span_id}-1",
            "traceparent": f"00-{trace_id}-{span_id}-01",
            "tracestate": f"sw8newrelic=1-{trace_id}-{span_id}",
            "uk": self.uk or random_string(65),
        }
        if self.token:
            headers["authorization"] = self.token
        if self.device_uk:
            headers["deviceUk"] = self.device_uk
            headers["devicetoken"] = self.device_uk
        if self.device_id or self.device_uk:
            device_id = self.device_id or self.device_uk
            headers["deviceId"] = device_id
            headers["deviceid"] = device_id
        if self.cookie:
            headers["cookie"] = self.cookie
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        require_auth: bool = False,
    ) -> dict[str, Any]:
        if require_auth and not self.token:
            raise YouPinError("缺少 YOUPIN_TOKEN，无法调用需要登录的悠悠有品接口。")

        body_payload = dict(payload)
        if self.device_uk and "Sessionid" not in body_payload:
            body_payload["Sessionid"] = self.device_uk

        url = urljoin(self.api_base, path.lstrip("/"))
        body: bytes | None = None
        if method == "GET":
            url = f"{url}?{urlencode(body_payload)}"
        else:
            body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=body, headers=self.headers(), method=method)

        try:
            with urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise YouPinError(f"HTTP {exc.code} {path}: {text[:300]}") from exc
        except URLError as exc:
            raise YouPinError(f"网络请求失败 {path}: {exc}") from exc
        except TimeoutError as exc:
            raise YouPinError(f"网络请求超时 {path}: {exc}") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise YouPinError(f"接口返回不是 JSON {path}: {text[:300]}") from exc

        code = response_code(data)
        msg = response_msg(data)
        if code in {84101, 84103, 84104}:
            if "savePurchaseOrder" in path:
                raise YouPinError(
                    f"悠悠有品新建求购接口被拒绝：code={code}, msg={msg}。"
                    "当前网页登录态可以读取/修改已有求购，但网页端提示“交易功能暂未开放”，"
                    "所以 savePurchaseOrder 新建接口会被服务端拒绝。"
                    "请先在 APP/客户端手动建立低价底单，或抓取 APP/客户端真实新建求购请求后再接入。"
                )
            raise YouPinError(
                f"悠悠有品登录态失效或设备信息不匹配：code={code}, msg={msg}。"
                "请重新登录 UU，更新 .env 里的 YOUPIN_TOKEN / YOUPIN_COOKIE / YOUPIN_DEVICE_TOKEN / "
                "YOUPIN_DEVICE_UK / YOUPIN_UK / YOUPIN_PC_UUID，然后在桌面端点击“重读 .env”或重启后台任务。"
            )
        return data

    def get(self, path: str, params: dict[str, Any], require_auth: bool = False) -> dict[str, Any]:
        return self._request_json("GET", path, params, require_auth=require_auth)

    def post(self, path: str, payload: dict[str, Any], require_auth: bool = False) -> dict[str, Any]:
        return self._request_json("POST", path, payload, require_auth=require_auth)

    def put(self, path: str, payload: dict[str, Any], require_auth: bool = False) -> dict[str, Any]:
        return self._request_json("PUT", path, payload, require_auth=require_auth)

    def search_templates(self, keyword: str, page_index: int, page_size: int) -> dict[str, Any]:
        return self.post(
            "/api/homepage/es/template/GetCsGoPagedList",
            {
                "listType": "10",
                "gameId": "730",
                "keyWords": keyword,
                "pageIndex": page_index,
                "pageSize": page_size,
                "sortType": "0",
                "listSortType": "1",
            },
            require_auth=True,
        )

    def get_sell_detail(
        self,
        template_id: int,
        page_index: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        return self.post(
            "/api/homepage/v2/detail/commodity/list/sell",
            {
                "templateId": template_id,
                "pageIndex": page_index,
                "pageSize": page_size,
                "sortType": 1,
            },
            require_auth=True,
        )

    def get_commodity_detail(self, commodity_id: int | str) -> dict[str, Any]:
        return self.get(
            "/api/commodity/Commodity/Detail",
            {
                "Id": commodity_id,
                "SupportShowImgHeight": 1,
                "SupportQueryStickerTemplateId": 1,
            },
            require_auth=True,
        )

    def get_template_purchase_orders(self, template_id: int) -> dict[str, Any]:
        return self.post(
            "https://pc-api.youpin898.com/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderListPC",
            {
                "templateId": template_id,
                "pageIndex": 1,
                "pageSize": 30,
                "minAbrade": 0,
                "maxAbrade": 1,
                "typeId": -1,
            },
            require_auth=True,
        )

    def search_my_purchase_orders(self, status: int = 20, page_index: int = 1, page_size: int = 40) -> dict[str, Any]:
        return self.post(
            "/api/youpin/bff/trade/purchase/order/searchPurchaseOrderList",
            {"pageIndex": page_index, "pageSize": page_size, "status": status},
            require_auth=True,
        )

    def get_inventory_list(self, page_index: int = 1, page_size: int = 50) -> dict[str, Any]:
        return self.post(
            "/api/youpin/pc/inventory/list",
            {"pageIndex": page_index, "pageSize": page_size, "gameId": 730},
            require_auth=True,
        )

    def get_inventory_v3(self, page_index: int = 1, page_size: int = 1000, refresh: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pageIndex": page_index,
            "pageSize": page_size,
            "AppType": 4,
            "IsMerge": 0,
        }
        if refresh:
            payload["IsRefresh"] = True
            payload["RefreshType"] = 2
        return self.post("/api/commodity/Inventory/GetUserInventoryDataListV3", payload, require_auth=True)

    def get_on_sale_commodity_list(self, template_id: int, page_index: int = 1, page_size: int = 10) -> dict[str, Any]:
        return self.post(
            "/api/homepage/pc/goods/market/queryOnSaleCommodityList",
            {"pageIndex": page_index, "pageSize": page_size, "templateId": str(template_id)},
            require_auth=True,
        )

    def sell_inventory_items(self, item_infos: list[dict[str, Any]]) -> dict[str, Any]:
        if not item_infos:
            raise YouPinError("没有可上架的库存物品。")
        return self.post(
            "/api/commodity/Inventory/SellInventoryWithLeaseV2",
            {"GameID": 730, "ItemInfos": item_infos},
            require_auth=True,
        )

    def off_shelf_commodity(self, commodity_id: int | str) -> dict[str, Any]:
        return self.put(
            "/api/commodity/Commodity/OffShelf",
            {
                "Ids": int(commodity_id),
                "IsForceOffline": True,
                "IsDeleteCommodityCache": 1,
            },
            require_auth=True,
        )

    def acknowledge_global_reminder(self, scene_code: str) -> dict[str, Any]:
        if not scene_code:
            raise YouPinError("缺少悠悠安全提示 sceneCode，无法确认。")
        return self.post(
            "/api/youpin/safe/user/global/remind/data/push",
            {"sceneCode": scene_code},
            require_auth=True,
        )

    def delete_purchase_orders(self, order_nos: list[str]) -> dict[str, Any]:
        order_no_list = [order_no for order_no in order_nos if order_no]
        if not order_no_list:
            raise YouPinError("没有可撤销的求购单编号。")
        return self.post(
            "/api/youpin/bff/trade/purchase/order/deletePurchaseOrder",
            {"orderNoList": order_no_list},
            require_auth=True,
        )

    def publish_purchase_order(
        self,
        template: StickerTemplate,
        purchase_price: Decimal,
        purchase_num: int,
        order_no: str = "",
        supply_quantity: int = 0,
    ) -> dict[str, Any]:
        price = float(purchase_price)
        total = round(price * purchase_num, 2)
        data: dict[str, Any] = {
            "templateId": template.template_id,
            "templateHashName": template.hash_name,
            "commodityName": template.name,
            "purchasePrice": price,
            "purchaseNum": purchase_num,
            "needPaymentAmount": total,
            "totalAmount": total,
            "incrementServiceCode": [1001],
            "priceDifference": 0,
            "discountAmount": 0,
            "payConfirmFlag": False,
            "repeatOrderCancelFlag": False,
        }

        path = "/api/youpin/bff/trade/purchase/order/savePurchaseOrder"
        if order_no:
            path = "/api/youpin/bff/trade/purchase/order/updatePurchaseOrder"
            data["orderNo"] = order_no
            data["templateName"] = template.name
            data["supplyQuantity"] = supply_quantity
        return self.post(path, data, require_auth=True)


__all__ = [
    "CleanupAction",
    "CyclePlan",
    "ExistingPurchaseOrder",
    "ExitAction",
    "InventoryItem",
    "MarketSnapshot",
    "PlanAction",
    "PurchaseRiskSnapshot",
    "StickerTemplate",
    "YouPinClient",
    "YouPinError",
    "deep_records",
    "money",
    "normalize_text",
    "parse_count_text",
    "pick",
    "response_code",
    "response_msg",
    "to_decimal",
]
