import logging

from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger('oscar_mws')


class AmazonStockTrackingMixin(object):
    """
    A mixin to make stock tracking for Amazon MWS fulfilled products possible.
    The way stock tracking works in Oscar doesn't play nicely with the details
    returned from MWS. Basically Amazon provides a single value which is the
    amount of items still available to be fulfilled. In Oscar, we track the
    number in stock as well as the allocated number of products.
    ``num_in_stock - num_allocated`` is the number of items actually avaiable
    to buy and both number in stock and number allocated are only decremented
    whenever an item is marked as shipped.

    To handle this properly and be able to synchronise the fulfillable number
    of products available from Amazon, we use this mixin to override the
    """

    def set_amazon_supply_quantity(self, quantity):
        """
        Convenience method to set the field ``num_in_stock`` to *quantity* and
        reset the allocated stock in ``num_allocated`` to zero. We don't care
        about allocation for MWS stock and therefore just reset it.
        """
        logger.info(
            'setting stock record to MWS supply quantity: {}'.format(quantity))
        self.num_in_stock = quantity
        self.num_allocated = 0
        self.save()

    def consume_allocation(self, quantity):
        """
        This is used when an item is shipped. We remove the original
        allocation and adjust the number in stock accordingly
        """
        if self.is_mws_record:
            logger.debug(
                'stock record is MWS record. Skipping consume allocation on '
                'stock record ', extra={'stockrecord_id': unicode(self.id)})
            return
        super(AmazonStockTrackingMixin, self).consume_allocation(quantity)
    consume_allocation.alters_data = True

    @property
    def is_mws_record(self):
        try:
            self.partner.amazon_merchant
        except ObjectDoesNotExist:
            return False
        else:
            return True
