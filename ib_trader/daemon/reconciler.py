"""IB reconciliation logic for the daemon.

Queries IB for status of all locally-tracked open orders.
For each discrepancy (IB shows filled/canceled but SQLite shows open):
- Updates SQLite to match IB reality.
- Logs RECONCILED_EXTERNAL with full detail.
"""
import logging
from decimal import Decimal

from ib_trader.config.context import AppContext
from ib_trader.data.models import OrderStatus

logger = logging.getLogger(__name__)

# IB statuses that mean the order is no longer working
FILLED_STATUSES = {"Filled"}
CANCELED_STATUSES = {"Cancelled", "Inactive", "ApiCancelled"}


async def run_reconciliation(ctx: AppContext) -> dict:
    """Query IB for all open orders and reconcile with SQLite.

    Args:
        ctx: Application dependency injection container.

    Returns:
        dict with 'changes' count and 'details' list of changed order IDs.
    """
    logger.info('{"event": "RECONCILIATION_STARTED"}')
    changes = []

    try:
        ib_orders = await ctx.ib.get_open_orders()
        ib_by_id = {o["ib_order_id"]: o for o in ib_orders}

        # Check all locally-tracked open orders against IB
        local_open = ctx.orders.get_all_open()

        for order in local_open:
            if not order.ib_order_id:
                continue

            ib_status = ib_by_id.get(order.ib_order_id)

            if ib_status is None:
                # Order not found in IB's open orders — may have been filled or canceled externally
                full_status = await ctx.ib.get_order_status(order.ib_order_id)
                ib_str = full_status["status"]
                qty_filled = full_status["qty_filled"]
                avg_price = full_status["avg_fill_price"]
                commission = full_status["commission"] or Decimal("0")

                if ib_str in FILLED_STATUSES and order.status not in (
                    OrderStatus.FILLED, OrderStatus.PARTIAL
                ):
                    ctx.orders.update_fill(order.id, qty_filled, avg_price or Decimal("0"), commission)
                    ctx.orders.update_status(order.id, OrderStatus.CLOSED_EXTERNAL)
                    logger.info(
                        '{"event": "RECONCILED_EXTERNAL", "order_id": "%s", '
                        '"symbol": "%s", "new_status": "CLOSED_EXTERNAL", '
                        '"qty_filled": "%s"}',
                        order.id, order.symbol, qty_filled,
                    )
                    changes.append(order.id)

                elif ib_str in CANCELED_STATUSES and order.status not in (
                    OrderStatus.CANCELED, OrderStatus.ABANDONED
                ):
                    ctx.orders.update_status(order.id, OrderStatus.CANCELED)
                    logger.info(
                        '{"event": "RECONCILED_EXTERNAL", "order_id": "%s", '
                        '"symbol": "%s", "new_status": "CANCELED"}',
                        order.id, order.symbol,
                    )
                    changes.append(order.id)

    except Exception as e:
        logger.error(
            '{"event": "RECONCILIATION_FAILED", "error": "%s"}', str(e), exc_info=True
        )
        return {"changes": 0, "details": [], "error": str(e)}

    result = {"changes": len(changes), "details": changes}
    logger.info(
        '{"event": "RECONCILIATION_COMPLETE", "changes": %d}', len(changes)
    )
    return result
