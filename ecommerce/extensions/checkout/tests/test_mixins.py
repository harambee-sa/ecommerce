"""
Tests for the ecommerce.extensions.checkout.mixins module.
"""
from decimal import Decimal

from mock import Mock, patch
from django.core import mail
from django.test import RequestFactory
from oscar.core.loading import get_model
from oscar.test import factories
from oscar.test.newfactories import BasketFactory, ProductFactory, UserFactory
from testfixtures import LogCapture
from waffle.models import Sample

from ecommerce.core.models import SegmentClient
from ecommerce.extensions.checkout.exceptions import BasketNotFreeError
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.fulfillment.status import ORDER
from ecommerce.extensions.refund.tests.mixins import RefundTestMixin
from ecommerce.tests.factories import SiteConfigurationFactory
from ecommerce.tests.mixins import BusinessIntelligenceMixin
from ecommerce.tests.testcases import TestCase

LOGGER_NAME = 'ecommerce.extensions.analytics.utils'
Basket = get_model('basket', 'Basket')


@patch.object(SegmentClient, 'track')
class EdxOrderPlacementMixinTests(BusinessIntelligenceMixin, RefundTestMixin, TestCase):
    """
    Tests validating generic behaviors of the EdxOrderPlacementMixin.
    """

    def setUp(self):
        super(EdxOrderPlacementMixinTests, self).setUp()

        self.user = UserFactory()
        self.order = self.create_order(status=ORDER.OPEN)

    def test_handle_payment_logging(self, __):
        """
        Ensure that we emit a log entry upon receipt of a payment notification.
        """
        amount = Decimal('9.99')
        basket_id = 'test-basket-id'
        currency = 'USD'
        processor_name = 'test-processor-name'
        reference = 'test-reference'
        user_id = '1'

        mock_source = Mock(currency=currency)
        mock_payment_event = Mock(
            amount=amount,
            processor_name=processor_name,
            reference=reference
        )
        mock_handle_payment_authorization_response = Mock(return_value=(mock_source, mock_payment_event))
        mock_payment_processor = Mock(handle_payment_authorization_response=mock_handle_payment_authorization_response)

        with patch('ecommerce.extensions.checkout.mixins.EdxOrderPlacementMixin.payment_processor',
                   mock_payment_processor):
            mock_basket = Mock(id=basket_id, owner=Mock(id=user_id))
            with LogCapture(LOGGER_NAME) as l:
                EdxOrderPlacementMixin().handle_payment(Mock(), mock_basket)
                l.check(
                    (
                        LOGGER_NAME,
                        'INFO',
                        'payment_received: amount="{}", basket_id="{}", currency="{}", '
                        'processor_name="{}", reference="{}", user_id="{}"'.format(
                            amount,
                            basket_id,
                            currency,
                            processor_name,
                            reference,
                            user_id
                        )
                    )
                )

    def test_handle_successful_order(self, mock_track):
        """
        Ensure that tracking events are fired with correct content when order
        placement event handling is invoked.
        """
        tracking_context = {'lms_user_id': 'test-user-id', 'lms_client_id': 'test-client-id', 'lms_ip': '127.0.0.1'}
        self.user.tracking_context = tracking_context
        self.user.save()

        with LogCapture(LOGGER_NAME) as l:
            EdxOrderPlacementMixin().handle_successful_order(self.order)
            # ensure event is being tracked
            self.assertTrue(mock_track.called)
            # ensure event data is correct
            self.assert_correct_event(
                mock_track,
                self.order,
                tracking_context['lms_user_id'],
                tracking_context['lms_client_id'],
                tracking_context['lms_ip'],
                self.order.number,
                self.order.currency,
                self.order.total_excl_tax
            )
            l.check(
                (
                    LOGGER_NAME,
                    'INFO',
                    'order_placed: amount="{}", basket_id="{}", contains_coupon="{}", currency="{}",'
                    ' order_number="{}", user_id="{}"'.format(
                        self.order.total_excl_tax,
                        self.order.basket.id,
                        self.order.contains_coupon,
                        self.order.currency,
                        self.order.number,
                        self.order.user.id
                    )
                )
            )

    def test_handle_successful_free_order(self, mock_track):
        """Verify that tracking events are not emitted for free orders."""
        order = self.create_order(free=True, status=ORDER.OPEN)
        EdxOrderPlacementMixin().handle_successful_order(order)

        # Verify that no event was emitted.
        self.assertFalse(mock_track.called)

    def test_handle_successful_order_no_context(self, mock_track):
        """
        Ensure that expected values are substituted when no tracking_context
        was available.
        """
        EdxOrderPlacementMixin().handle_successful_order(self.order)
        # ensure event is being tracked
        self.assertTrue(mock_track.called)
        # ensure event data is correct
        self.assert_correct_event(
            mock_track,
            self.order,
            'ecommerce-{}'.format(self.user.id),
            None,
            None,
            self.order.number,
            self.order.currency,
            self.order.total_excl_tax
        )

    def test_handle_successful_order_no_segment_key(self, mock_track):
        """
        Ensure that tracking events do not fire when there is no Segment key
        configured.
        """
        self.site.siteconfiguration.segment_key = None
        EdxOrderPlacementMixin().handle_successful_order(self.order)
        # ensure no event was fired
        self.assertFalse(mock_track.called)

    def test_handle_successful_order_segment_error(self, mock_track):
        """
        Ensure that exceptions raised while emitting tracking events are
        logged, but do not otherwise interrupt program flow.
        """
        with patch('ecommerce.extensions.analytics.utils.logger.exception') as mock_log_exc:
            mock_track.side_effect = Exception("clunk")
            EdxOrderPlacementMixin().handle_successful_order(self.order)
        # ensure that analytics.track was called, but the exception was caught
        self.assertTrue(mock_track.called)
        # ensure we logged a warning.
        self.assertTrue(mock_log_exc.called_with("Failed to emit tracking event upon order placement."))

    def test_handle_successful_async_order(self, __):
        """
        Verify that a Waffle Sample can be used to control async order fulfillment.
        """
        sample, created = Sample.objects.get_or_create(
            name='async_order_fulfillment',
            defaults={
                'percent': 100.0,
                'note': 'Determines what percentage of orders are fulfilled asynchronously.',
            }
        )

        if not created:
            sample.percent = 100.0
            sample.save()

        with patch('ecommerce.extensions.checkout.mixins.fulfill_order.delay') as mock_delay:
            EdxOrderPlacementMixin().handle_successful_order(self.order)
            self.assertTrue(mock_delay.called)
            mock_delay.assert_called_once_with(self.order.number, site_code='edX')

    def test_place_free_order(self, __):
        """ Verify an order is placed and the basket is submitted. """
        basket = BasketFactory(owner=self.user, site=self.site)
        basket.add_product(ProductFactory(stockrecords__price_excl_tax=0))
        order = EdxOrderPlacementMixin().place_free_order(basket)

        self.assertIsNotNone(order)
        self.assertEqual(basket.status, Basket.SUBMITTED)

    def test_non_free_basket_order(self, __):
        """ Verify an error is raised for non-free basket. """
        basket = BasketFactory(owner=self.user, site=self.site)
        basket.add_product(ProductFactory(stockrecords__price_excl_tax=10))

        with self.assertRaises(BasketNotFreeError):
            EdxOrderPlacementMixin().place_free_order(basket)

    def test_send_confirmation_message(self, __):
        """
        Verify the send confirmation message override functions as expected
        """
        request = RequestFactory()
        user = self.create_user()
        user.email = 'test_user@example.com'
        request.user = user
        site_from_email = 'from@example.com'
        site_configuration = SiteConfigurationFactory(partner__name='Tester', from_email=site_from_email)
        request.site = site_configuration.site
        order = factories.create_order()
        order.user = user
        mixin = EdxOrderPlacementMixin()
        mixin.request = request

        # Happy path
        mixin.send_confirmation_message(order, 'ORDER_PLACED', request.site)
        self.assertEqual(mail.outbox[0].from_email, site_from_email)
        mail.outbox = []

        # Invalid code path (graceful exit)
        mixin.send_confirmation_message(order, 'INVALID_CODE', request.site)
        self.assertEqual(len(mail.outbox), 0)

        # Invalid messages container path (graceful exit)
        with patch('ecommerce.extensions.checkout.mixins.CommunicationEventType.objects.get') as mock_get:
            mock_event_type = Mock()
            mock_event_type.get_messages.return_value = {}
            mock_get.return_value = mock_event_type
            mixin.send_confirmation_message(order, 'ORDER_PLACED', request.site)
            self.assertEqual(len(mail.outbox), 0)

            mock_event_type.get_messages.return_value = {'body': None}
            mock_get.return_value = mock_event_type
            mixin.send_confirmation_message(order, 'ORDER_PLACED', request.site)
            self.assertEqual(len(mail.outbox), 0)
