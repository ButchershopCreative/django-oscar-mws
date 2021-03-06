import logging

from collections import defaultdict
from dateutil import parser as du_parser

from django.db.models import get_model
from django.core.exceptions import ObjectDoesNotExist

from oscar.core.loading import get_class

from ..api import MWSObject, MWSError
from ..connection import get_merchant_connection

logger = logging.getLogger('oscar_mws')

Partner = get_model('partner', 'Partner')
Product = get_model('catalogue', 'Product')
StockRecord = get_model('partner', 'StockRecord')

Line = get_model('order', 'Line')
ShippingEvent = get_model('order', 'ShippingEvent')
ShippingEventType = get_model('order', 'ShippingEventType')
ShippingEventQuantity = get_model('order', 'ShippingEventQuantity')
EventHandler = get_class('order.processing', 'EventHandler')

ShipmentPackage = get_model('oscar_mws', 'ShipmentPackage')
FulfillmentOrder = get_model('oscar_mws', 'FulfillmentOrder')
FulfillmentOrderLine = get_model('oscar_mws', 'FulfillmentOrderLine')
FulfillmentShipment = get_model('oscar_mws', 'FulfillmentShipment')


def _update_shipment(shipment_data, fulfillment_order):
    try:
        shipment = FulfillmentShipment.objects.get(
            shipment_id=shipment_data.AmazonShipmentId
        )
    except FulfillmentShipment.DoesNotExist:
        shipment = FulfillmentShipment.objects.create(
            shipment_id=shipment_data.AmazonShipmentId,
            order=fulfillment_order.order,
        )

    has_status_changed = bool(
        shipment.status != shipment_data.FulfillmentShipmentStatus
    )

    shipment.fulfillment_center_id = shipment_data.FulfillmentCenterId
    shipment.status = shipment_data.FulfillmentShipmentStatus

    if shipment_data.EstimatedArrivalDateTime:
        shipment.date_estimated_arrival = du_parser.parse(
            shipment_data.EstimatedArrivalDateTime
        )

    if shipment_data.ShippingDateTime:
        shipment.date_shipped = du_parser.parse(
            shipment_data.ShippingDateTime
        )
    shipment.save()

    if not has_status_changed:
        logger.info('status for fulfillment shipment unchanged, '
                    'no shipping event created', extra={
                'fulfillment_id': fulfillment_order.fulfillment_id,
                'shipment_id': shipment.shipment_id,
        })
        return

    event_type, __ = ShippingEventType.objects.get_or_create(
        name=shipment_data.FulfillmentShipmentStatus
    )

    shipping_note = []
    packages = shipment_data.get('FulfillmentShipmentPackage') or MWSObject()
    for fpackage in packages.get_list('member'):
        ShipmentPackage.objects.get_or_create(
            package_number=fpackage.PackageNumber,
            tracking_number=fpackage.TrackingNumber,
            carrier_code=fpackage.CarrierCode,
            fulfillment_shipment=shipment,
        )
        shipping_note.append(
            '* Shipped package via {0} with tracking number {1}'.format(
                fpackage.CarrierCode,
                fpackage.TrackingNumber,
            )
        )

    event_handler = EventHandler()

    items = shipment_data.get('FulfillmentShipmentItem') or MWSObject()
    for item in items.get_list('member'):
        fulfillment_lines = FulfillmentOrderLine.objects.filter(
            fulfillment_order=fulfillment_order,
            order_item_id=item.SellerFulfillmentOrderItemId)

        shipping_lines = []
        shipping_quantities = []
        quantity = int(item.Quantity)
        for fline in fulfillment_lines:
            if quantity <= 0:
                break
            if quantity >= fline.quantity:
                shipped_quantity = fline.quantity
            else:
                shipped_quantity = quantity
            shipping_lines.append(fline.line)
            shipping_quantities.append(shipped_quantity)

            quantity = quantity - shipped_quantity
            fline.shipment = shipment
            try:
                fline.package = shipment.packages.get(
                    package_number=item.PackageNumber)
            except ShipmentPackage.DoesNotExist:
                pass
            fline.save()

        if shipping_note:
            reference = '\n'.join(shipping_note)
        else:
            reference = None

        event_handler.validate_shipping_event(
            order=fulfillment_order.order, event_type=event_type,
            lines=shipping_lines, line_quantities=shipping_quantities,
            reference=reference)
        event = event_handler.create_shipping_event(
            order=fulfillment_order.order, event_type=event_type,
            lines=shipping_lines, line_quantities=shipping_quantities,
            reference=reference)
        shipment.shipment_events.add(event)


def submit_fulfillment_orders(orders):
    for order in orders:
        submit_fulfillment_order(order)


def submit_fulfillment_order(fulfillment_order):
    outbound_api = get_merchant_connection(
        merchant_id=fulfillment_order.merchant.seller_id
    ).outbound
    try:
        outbound_api.create_fulfillment_order(
            **fulfillment_order.get_order_kwargs())
    except MWSError:
        fulfillment_order.status = fulfillment_order.SUBMISSION_FAILED
    else:
        fulfillment_order.status = fulfillment_order.SUBMITTED
    fulfillment_order.save()


def update_fulfillment_order(fulfillment_order):
    """
    Update the provided *fulfillment_order* with the latest details from MWS.
    Details for the given *fulfillment_order* are requested from MWS, parsed
    and stored in the database.
    It creates/updates ``FulfillmentOrder``, ``FulfillmentShipment`` and
    ``FulfillmentShipmentPackage`` as well as affected ``FulfillmentOrderLine``
    items. As a side-effect, a ``ShippingEvent`` is created for every shipment
    item received from MWS that changes the status of the corresponding model.
    """
    outbound_api = get_merchant_connection(
        fulfillment_order.merchant.seller_id
    ).outbound
    try:
        response = outbound_api.get_fulfillment_order(
            order_id=fulfillment_order.fulfillment_id,
        ).parsed
    except MWSError:
        logger.error(
            "updating fulfillment order failed", exc_info=1,
            extra={'fulfillment_id': fulfillment_order.fulfillment_id})
        return

    forder = response.FulfillmentOrder
    fulfillment_order.status = forder.FulfillmentOrderStatus
    fulfillment_order.save()

    shipments = response.get('FulfillmentShipment') or MWSObject()
    for fshipment in shipments.get_list('member'):
        _update_shipment(fshipment, fulfillment_order)
    return fulfillment_order


def update_fulfillment_orders(fulfillment_orders):
    processed_orders = []
    for order in fulfillment_orders:
        processed_orders.append(update_fulfillment_order(order))
    return processed_orders


def get_all_fulfillment_orders(merchant, query_datetime=None):
    kwargs = {}
    if query_datetime:
        kwargs['QueryStartDateTime'] = query_datetime.isoformat()

    try:
        response = get_merchant_connection(
            merchant.seller_id
        ).list_all_fulfillment_orders(**kwargs)
    except MWSError:
        logger.error("requesting all fulfillment orders failed", exc_info=1)
        return []

    processed_orders = []
    for forder in response.ListAllFulfillmentOrdersResult.FulfillmentOrders:
        fulfillment_order, __ = FulfillmentOrder.objects.get_or_create(
            fulfillment_id=forder.SellerFulfillmentOrderId
        )

        reported_date = du_parser.parse(forder.StatusUpdatedDateTime)
        if reported_date == fulfillment_order.date_updated:
            return fulfillment_order

        fulfillment_order.status = forder.FulfillmentOrderStatus
        fulfillment_order.save()
    return processed_orders


def update_inventory(products):
    product_values = Product.objects.filter(
        id__in=[p.id for p in products],
    ).values_list(
        'amazon_profile__sku',
        'amazon_profile__marketplaces__merchant__seller_id'
    )
    submit_products = defaultdict(set)
    for sku, seller_id in product_values:
        if seller_id is None:
            logger.info(
                'Product with SKU {} has no seller account'.format(sku)
            )
            continue
        submit_products[seller_id].add(sku)

    for seller_id, skus in submit_products.iteritems():
        inventory_api = get_merchant_connection(seller_id).inventory
        response = inventory_api.list_inventory_supply(skus=skus).parsed

        for inventory in response.InventorySupplyList.get_list('member'):
            try:
                stockrecord = StockRecord.objects.get(
                    product__amazon_profile__sku=inventory.SellerSKU,
                    partner__amazon_merchant__seller_id=seller_id)
            except StockRecord.DoesNotExist:
                # It seems that there's no stock record available for the
                # product that is linked to the merchant account. Let's try
                # and create a new stockrecord for this product and merchant's
                # partner. If that fails, we are out of options and just
                # continue with the next one
                try:
                    stockrecord = StockRecord.objects.create(
                        product=Product.objects.get(
                            amazon_profile__sku=inventory.SellerSKU),
                        partner=Partner.objects.get(
                            amazon_merchant__seller_id=seller_id),
                    )
                except ObjectDoesNotExist:
                    logger.error(
                        "no stockrecord and partner found for given product "
                        "and merchant", extra={
                            'seller_sku': inventory.SellerSKU,
                            'seller_id': seller_id})
                    continue

            stockrecord.set_amazon_supply_quantity(
                inventory.InStockSupplyQuantity)
            stockrecord.save()
