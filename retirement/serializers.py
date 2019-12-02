from datetime import timedelta
from decimal import Decimal
import json
import requests
import traceback

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.mail import mail_admins
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse
from rest_framework.validators import UniqueValidator

from blitz_api.services import (check_if_translated_field,
                                remove_translation_fields,
                                getMessageTranslate)
from log_management.models import Log
from retirement.services import refund_retreat
from store.exceptions import PaymentAPIError
from store.models import Order, OrderLine, PaymentProfile, Refund
from store.serializers import BaseProductSerializer, CouponSerializer
from store.services import (charge_payment,
                            create_external_payment_profile,
                            create_external_card,
                            PAYSAFE_CARD_TYPE,
                            PAYSAFE_EXCEPTION,
                            refund_amount, )

from .fields import TimezoneField
from .models import (Picture, Reservation, Retreat, WaitQueue,
                     WaitQueueNotification, RetreatInvitation)

User = get_user_model()

TAX_RATE = settings.LOCAL_SETTINGS['SELLING_TAX']


class RetreatSerializer(BaseProductSerializer):
    places_remaining = serializers.ReadOnlyField()
    total_reservations = serializers.ReadOnlyField()
    reservations = serializers.SerializerMethodField()
    reservations_canceled = serializers.SerializerMethodField()
    timezone = TimezoneField(
        required=True,
        help_text=_("Timezone of the workplace."),
    )
    name = serializers.CharField(
        required=False,
        validators=[UniqueValidator(queryset=Retreat.objects.all())],
    )
    name_fr = serializers.CharField(
        required=False,
        allow_null=True,
        validators=[UniqueValidator(queryset=Retreat.objects.all())],
    )
    name_en = serializers.CharField(
        required=False,
        allow_null=True,
        validators=[UniqueValidator(queryset=Retreat.objects.all())],
    )
    details = serializers.CharField(required=False, )
    country = serializers.CharField(required=False, )
    state_province = serializers.CharField(required=False, )
    city = serializers.CharField(required=False, )
    address_line1 = serializers.CharField(required=False, )

    available = serializers.BooleanField(
        required=False
    )
    # June 7th 2018
    # The SlugRelatedField serializer can't get a field's attributes.
    # Ex: It can't get the "url" attribute of Imagefield Picture.picture.url
    # So here is a workaround: a SerializerMethodField is used to manually get
    # picture urls. This works but is not as clean as it could be.
    # Note: this is a read-only field so it isn't used for Workplace creation.
    pictures = serializers.SerializerMethodField()

    def validate_refund_rate(self, value):
        if value > 100:
            raise serializers.ValidationError(_(
                "Refund rate must be between 0 and 100 (%)."
            ))
        return value

    def get_pictures(self, obj):
        request = self.context['request']
        picture_urls = [picture.picture.url for picture in obj.pictures.all()]
        return [request.build_absolute_uri(url) for url in picture_urls]

    def get_reservations(self, obj):
        reservation_ids = Reservation.objects.filter(
            is_active=True,
            retreat=obj,
        ).values_list(
            'id',
            flat=True,
        )
        return [
            reverse(
                'retreat:reservation-detail',
                args=[id],
                request=self.context['request'],
            ) for id in reservation_ids
        ]

    def get_reservations_canceled(self, obj):
        reservation_ids = Reservation.objects.filter(
            is_active=False,
            retreat=obj,
        ).values_list(
            'id',
            flat=True,
        )
        return [
            reverse(
                'retreat:reservation-detail',
                args=[id],
                request=self.context['request'],
            ) for id in reservation_ids
        ]

    def validate(self, attr):
        err = {}
        if not check_if_translated_field('name', attr):
            err.update(getMessageTranslate('name', attr, True))
        if not check_if_translated_field('details', attr):
            err.update(getMessageTranslate('details', attr, True))
        if not check_if_translated_field('country', attr):
            err.update(getMessageTranslate('country', attr, True))
        if not check_if_translated_field('state_province', attr):
            err.update(getMessageTranslate('state_province', attr, True))
        if not check_if_translated_field('city', attr):
            err.update(getMessageTranslate('city', attr, True))
        if not check_if_translated_field('address_line1', attr):
            err.update(getMessageTranslate('address_line1', attr, True))
        if err:
            raise serializers.ValidationError(err)
        return super(RetreatSerializer, self).validate(attr)

    def create(self, validated_data):
        """
        Schedule retreat reminder and post-event emails.
        UPDATE: Commenting out reminder email since they are no longer desired.
        """
        retreat = super().create(validated_data)

        scheduler_url = '{0}'.format(
            settings.EXTERNAL_SCHEDULER['URL'],
        )

        # Set reminder email
        reminder_date = validated_data['start_time'] - timedelta(days=7)

        data = {
            "hour": 8,
            "minute": 0,
            "day_of_month": reminder_date.day,
            "month": reminder_date.month,
            "url": '{0}{1}'.format(
                self.context['request'].build_absolute_uri(
                    reverse(
                        'retreat:retreat-detail',
                        args=(retreat.id, )
                    )
                ),
                "/remind_users"
            ),
            "description": "Retreat 7-days reminder notification"
        }

        try:
            auth_data = {
                "username": settings.EXTERNAL_SCHEDULER['USER'],
                "password": settings.EXTERNAL_SCHEDULER['PASSWORD']
            }
            auth = requests.post(
                scheduler_url + "/authentication",
                json=auth_data,
            )
            auth.raise_for_status()

            r = requests.post(
                scheduler_url + "/tasks",
                json=data,
                headers={
                    'Authorization':
                    'Token ' + json.loads(auth.content)['token']},
            )
            r.raise_for_status()
        except (requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError) as err:
            mail_admins(
                "Thèsez-vous: external scheduler error",
                "{0}\nRetreat:{1}\nException:\n{2}\n".format(
                    "Pre-event email task scheduling failed!",
                    retreat.__dict__,
                    traceback.format_exc(),
                )
            )

        # Set post-event email
        # Send the email at midnight the next day.
        throwback_date = validated_data['end_time'] + timedelta(days=1)

        data = {
            "hour": 0,
            "minute": 0,
            "day_of_month": throwback_date.day,
            "month": throwback_date.month,
            "url": '{0}{1}'.format(
                self.context['request'].build_absolute_uri(
                    reverse(
                        'retreat:retreat-detail',
                        args=(retreat.id, )
                    )
                ),
                "/recap"
            ),
            "description": "Retreat post-event notification"
        }

        try:
            auth_data = {
                "username": settings.EXTERNAL_SCHEDULER['USER'],
                "password": settings.EXTERNAL_SCHEDULER['PASSWORD']
            }
            auth = requests.post(
                scheduler_url + "/authentication",
                json=auth_data,
            )
            auth.raise_for_status()

            r = requests.post(
                scheduler_url + "/tasks",
                json=data,
                headers={
                    'Authorization':
                    'Token ' + json.loads(auth.content)['token']},
                timeout=(10, 10),
            )
            r.raise_for_status()
        except (requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError) as err:
            mail_admins(
                "Thèsez-vous: external scheduler error",
                "{0}\nRetreat:{1}\nException:\n{2}\n".format(
                    "Post-event email task scheduling failed!",
                    retreat.__dict__,
                    traceback.format_exc(),
                )
            )

        return retreat

    def to_representation(self, instance):
        is_staff = self.context['request'].user.is_staff
        if self.context['view'].action == 'retrieve' and is_staff:
            from blitz_api.serializers import UserSerializer
            self.fields['users'] = UserSerializer(many=True)
        data = super(RetreatSerializer, self).to_representation(instance)

        # We don't need orderlines for retreat in this serializer
        if data.get("order_lines") is not None:
            data.pop("order_lines")

        # TODO put back available after migration from is_active
        data.pop("available")

        if is_staff:
            return data
        return remove_translation_fields(data)

    class Meta:
        model = Retreat
        exclude = ('deleted',)
        extra_kwargs = {
            'details': {
                'help_text': _("Description of the retreat.")
            },
            'name': {
                'help_text': _("Name of the retreat."),
                'validators':
                [UniqueValidator(queryset=Retreat.objects.all())],
            },
            'seats': {
                'help_text': _("Number of available seats.")
            },
            'url': {
                'view_name': 'retreat:retreat-detail',
            },
        }


class PictureSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()

    def to_representation(self, instance):
        data = super(PictureSerializer, self).to_representation(instance)
        if self.context['request'].user.is_staff:
            return data
        return remove_translation_fields(data)

    class Meta:
        model = Picture
        fields = '__all__'
        extra_kwargs = {
            'retreat': {
                'help_text': _("Retreat represented by the picture."),
                'view_name': 'retreat:retreat-detail',
            },
            'url': {
                'view_name': 'retreat:picture-detail',
            },
            'name': {
                'help_text': _("Name of the picture."),
            },
            'picture': {
                'help_text': _("File to upload."),
            }
        }


class ReservationSerializer(serializers.HyperlinkedModelSerializer):
    from blitz_api.serializers import UserSerializer
    id = serializers.ReadOnlyField()
    # Custom names are needed to overcome an issue with DRF:
    # https://github.com/encode/django-rest-framework/issues/2719
    # I
    retreat_details = RetreatSerializer(
        read_only=True,
        source='retreat',
    )
    user_details = UserSerializer(
        read_only=True,
        source='user',
    )
    payment_token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    single_use_token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    def validate(self, attrs):
        """Prevents overlapping reservations."""
        validated_data = super(ReservationSerializer, self).validate(attrs)

        action = self.context['view'].action

        # This validation is here instead of being in the 'update()' method
        # because we need to stop validation if improper fields are passed in
        # a partial update.
        if action == 'partial_update':
            # Only allow modification of is_present & retreat fields.
            is_invalid = validated_data.copy()
            is_invalid.pop('is_present', None)
            is_invalid.pop('retreat', None)
            is_invalid.pop('payment_token', None)
            is_invalid.pop('single_use_token', None)
            if is_invalid:
                raise serializers.ValidationError({
                    'non_field_errors': [
                        _("Only is_present and retreat can be updated. To "
                          "change other fields, delete this reservation and "
                          "create a new one.")
                    ]
                })
            return attrs

        # Generate a list of tuples containing start/end time of
        # existing reservations.
        start = validated_data['retreat'].start_time
        end = validated_data['retreat'].end_time
        active_reservations = Reservation.objects.filter(
            user=validated_data['user'],
            is_active=True,
        )

        active_reservations = active_reservations.values_list(
            'retreat__start_time',
            'retreat__end_time',

        )

        for retreats in active_reservations:
            if max(retreats[0], start) < min(retreats[1], end):
                raise serializers.ValidationError({
                    'non_field_errors': [_(
                        "This reservation overlaps with another active "
                        "reservations for this user."
                    )]
                })
        return attrs

    def create(self, validated_data):
        """
        Allows an admin to create retreats reservations for another user.
        """
        validated_data['refundable'] = False
        validated_data['exchangeable'] = False
        validated_data['is_active'] = True

        if validated_data['retreat'].places_remaining <= 0:
            raise serializers.ValidationError({
                'non_field_errors': [_(
                    "This retreat doesn't have available places. Please "
                    "check number of seats available and reserved seats."
                )]
            })

        return super().create(validated_data)

    def update(self, instance, validated_data):

        if not instance.exchangeable and validated_data.get('retreat'):
            raise serializers.ValidationError({
                'non_field_errors': [_(
                    "This reservation is not exchangeable. Please contact us "
                    "to make any changes to this reservation."
                )]
            })

        user = instance.user
        payment_token = validated_data.pop('payment_token', None)
        single_use_token = validated_data.pop('single_use_token', None)
        need_transaction = False
        need_refund = False
        amount = 0
        profile = PaymentProfile.objects.filter(owner=user).first()
        instance_pk = instance.pk
        current_retreat: Retreat = instance.retreat
        coupon = instance.order_line.coupon
        coupon_value = instance.order_line.coupon_real_value
        order_line = instance.order_line
        request = self.context['request']

        if not self.context['request'].user.is_staff:
            validated_data.pop('is_present', None)

        if not instance.is_active:
            raise serializers.ValidationError({
                'non_field_errors': [_(
                    "This reservation has already been canceled."
                )]
            })

        with transaction.atomic():
            # NOTE: This copy logic should probably be inside the "if" below
            #       that checks if a retreat exchange is done.
            # Create a copy of the reservation. This copy keeps track of
            # the exchange.
            canceled_reservation = instance
            canceled_reservation.pk = None
            canceled_reservation.save()

            instance = Reservation.objects.get(id=instance_pk)

            canceled_reservation.is_active = False
            canceled_reservation.cancelation_reason = 'U'
            canceled_reservation.cancelation_action = 'E'
            canceled_reservation.cancelation_date = timezone.now()
            canceled_reservation.save()

            # Update the reservation
            instance = super(ReservationSerializer, self).update(
                instance,
                validated_data,
            )

            # Update retreat seats
            free_seats = (
                current_retreat.seats -
                current_retreat.total_reservations
            )
            if (current_retreat.reserved_seats or free_seats == 1):
                current_retreat.reserved_seats += 1
                current_retreat.save()

            if validated_data.get('retreat'):
                # Validate if user has the right to reserve a seat in the new
                # retreat
                new_retreat = instance.retreat
                old_retreat = current_retreat

                user_waiting = new_retreat.wait_queue.filter(user=user)
                free_seats = (
                    new_retreat.seats -
                    new_retreat.total_reservations -
                    new_retreat.reserved_seats +
                    1
                )
                reserved_for_user = (
                    new_retreat.reserved_seats and
                    WaitQueueNotification.objects.filter(
                        user=user,
                        retreat=new_retreat
                    )
                )
                if not (free_seats > 0 or reserved_for_user):
                    raise serializers.ValidationError({
                        'non_field_errors': [_(
                            "There are no places left in the requested "
                            "retreat."
                        )]
                    })
                if user_waiting:
                    user_waiting.delete()

            if (self.context['view'].action == 'partial_update' and
                    validated_data.get('retreat')):
                if order_line.quantity > 1:
                    raise serializers.ValidationError({
                        'non_field_errors': [_(
                            "The order containing this reservation has a "
                            "quantity bigger than 1. Please contact the "
                            "support team."
                        )]
                    })
                days_remaining = current_retreat.start_time - timezone.now()
                days_exchange = timedelta(
                    days=current_retreat.min_day_exchange
                )
                respects_minimum_days = (days_remaining >= days_exchange)
                new_retreat_price = validated_data['retreat'].price
                if current_retreat.price < new_retreat_price:
                    # If the new retreat is more expensive, reapply the
                    # coupon on the new orderline created. In other words, any
                    # coupon used for the initial purchase is applied again
                    # here.
                    need_transaction = True
                    amount = (
                        validated_data['retreat'].price -
                        order_line.coupon_real_value
                    )
                    if not (payment_token or single_use_token):
                        raise serializers.ValidationError({
                            'non_field_errors': [_(
                                "The new retreat is more expensive than "
                                "the current one. Provide a payment_token or "
                                "single_use_token to charge the balance."
                            )]
                        })
                if current_retreat.price > new_retreat_price:
                    # If a coupon was applied for the purchase, check if the
                    # real cost of the purchase was lower than the price
                    # difference.
                    # If so, refund the real cost of the purchase.
                    # Else refund the difference between the 2 retreats.
                    need_refund = True
                    price_diff = (
                        current_retreat.price -
                        validated_data['retreat'].price
                    )
                    real_cost = order_line.cost
                    amount = min(price_diff, real_cost)
                if current_retreat == validated_data['retreat']:
                    raise serializers.ValidationError({
                        'retreat': [_(
                            "That retreat is already assigned to this "
                            "object."
                        )]
                    })
                if not respects_minimum_days:
                    raise serializers.ValidationError({
                        'non_field_errors': [_(
                            "Maximum exchange date exceeded."
                        )]
                    })
                if need_transaction and (single_use_token and not profile):
                    # Create external profile
                    try:
                        create_profile_res = create_external_payment_profile(
                            user
                        )
                    except PaymentAPIError as err:
                        raise serializers.ValidationError({
                            'message': err,
                            'detail': err.detail
                        })
                    # Create local profile
                    profile = PaymentProfile.objects.create(
                        name="Paysafe",
                        owner=user,
                        external_api_id=create_profile_res.json()['id'],
                        external_api_url='{0}{1}'.format(
                            create_profile_res.url,
                            create_profile_res.json()['id']
                        )
                    )
                # Generate a list of tuples containing start/end time of
                # existing reservations.
                start = validated_data['retreat'].start_time
                end = validated_data['retreat'].end_time
                active_reservations = Reservation.objects.filter(
                    user=user,
                    is_active=True,
                ).exclude(pk=instance.pk).values_list(
                    'retreat__start_time',
                    'retreat__end_time',
                )

                for retreats in active_reservations:
                    if max(retreats[0], start) < min(retreats[1], end):
                        raise serializers.ValidationError({
                            'non_field_errors': [_(
                                "This reservation overlaps with another "
                                "active reservations for this user."
                            )]
                        })
                if need_transaction:
                    order = Order.objects.create(
                        user=user,
                        transaction_date=timezone.now(),
                        authorization_id=1,
                        settlement_id=1,
                    )
                    new_order_line = OrderLine.objects.create(
                        order=order,
                        quantity=1,
                        content_type=ContentType.objects.get_for_model(
                            Retreat
                        ),
                        object_id=validated_data['retreat'].id,
                        coupon=coupon,
                        coupon_real_value=coupon_value,
                    )
                    tax = round(amount * Decimal(TAX_RATE), 2)
                    amount *= Decimal(TAX_RATE + 1)
                    amount = round(amount * 100, 2)
                    retreat = validated_data['retreat']

                    # Do a complete refund of the previous retreat
                    try:
                        refund_instance = refund_retreat(
                            canceled_reservation,
                            100,
                            "Exchange retreat {0} for retreat "
                            "{1}".format(
                                str(current_retreat),
                                str(validated_data['retreat'])
                            )
                        )
                    except PaymentAPIError as err:
                        if str(err) == PAYSAFE_EXCEPTION['3406']:
                            raise serializers.ValidationError({
                                'non_field_errors': [_(
                                    "The order has not been charged yet. "
                                    "Try again later."
                                )],
                                'detail': err.detail
                            })
                        raise serializers.ValidationError({
                            'message': str(err),
                            'detail': err.detail
                        })

                    if payment_token and int(amount):
                        # Charge the order with the external payment API
                        try:
                            charge_response = charge_payment(
                                int(round(amount)),
                                payment_token,
                                str(order.id)
                            )
                        except PaymentAPIError as err:
                            raise serializers.ValidationError({
                                'message': err,
                                'detail': err.detail
                            })

                    elif single_use_token and int(amount):
                        # Add card to the external profile & charge user
                        try:
                            card_create_response = create_external_card(
                                profile.external_api_id,
                                single_use_token
                            )
                            charge_response = charge_payment(
                                int(round(amount)),
                                card_create_response.json()['paymentToken'],
                                str(order.id)
                            )
                        except PaymentAPIError as err:
                            raise serializers.ValidationError({
                                'message': err,
                                'detail': err.detail
                            })
                    charge_res_content = charge_response.json()
                    order.authorization_id = charge_res_content['id']
                    order.settlement_id = charge_res_content['settlements'][0][
                        'id'
                    ]
                    order.reference_number = charge_res_content[
                        'merchantRefNum'
                    ]
                    order.save()
                    instance.order_line = new_order_line
                    instance.save()

                if need_refund:
                    tax = round(amount * Decimal(TAX_RATE), 2)
                    amount *= Decimal(TAX_RATE + 1)
                    amount = round(amount * 100, 2)
                    retreat = validated_data['retreat']

                    refund_instance = Refund.objects.create(
                        orderline=order_line,
                        refund_date=timezone.now(),
                        amount=amount/100,
                        details="Exchange retreat {0} for "
                                "retreat {1}".format(
                                    str(current_retreat),
                                    str(validated_data['retreat'])
                                ),
                    )

                    try:
                        refund_response = refund_amount(
                            order_line.order.settlement_id,
                            int(round(amount))
                        )
                        refund_res_content = refund_response.json()
                        refund_instance.refund_id = refund_res_content['id']
                        refund_instance.save()
                    except PaymentAPIError as err:
                        if str(err) == PAYSAFE_EXCEPTION['3406']:
                            raise serializers.ValidationError({
                                'non_field_errors': [_(
                                    "The order has not been charged yet. "
                                    "Try again later."
                                )],
                                'detail': err.detail
                            })
                        raise serializers.ValidationError({
                            'message': str(err),
                            'detail': err.detail
                        })

                    new_retreat = retreat
                    old_retreat = current_retreat

            retrat_notification_url = request.build_absolute_uri(
                reverse(
                    'retreat:retreat-notify',
                    args=[current_retreat.id]
                )
            )
            current_retreat.notify_scheduler_waite_queue(
                retrat_notification_url)

        # Send appropriate emails
        # Send order confirmation email
        if need_transaction:
            items = [
                {
                    'price': new_order_line.content_object.price,
                    'name': "{0}: {1}".format(
                        str(new_order_line.content_type),
                        new_order_line.content_object.name
                    ),
                }
            ]

            merge_data = {
                'STATUS': "APPROUVÉE",
                'CARD_NUMBER':
                charge_res_content['card']['lastDigits'],
                'CARD_TYPE': PAYSAFE_CARD_TYPE[
                    charge_res_content['card']['type']
                ],
                'DATETIME': timezone.localtime().strftime("%x %X"),
                'ORDER_ID': order.id,
                'CUSTOMER_NAME':
                    user.first_name + " " + user.last_name,
                'CUSTOMER_EMAIL': user.email,
                'CUSTOMER_NUMBER': user.id,
                'AUTHORIZATION': order.authorization_id,
                'TYPE': "Achat",
                'ITEM_LIST': items,
                'TAX': round(
                    (new_order_line.cost - current_retreat.price) *
                    Decimal(TAX_RATE),
                    2,
                ),
                'DISCOUNT': current_retreat.price,
                'COUPON': {'code': _("Échange")},
                'SUBTOTAL': round(
                    new_order_line.cost - current_retreat.price,
                    2
                ),
                'COST': round(
                    (new_order_line.cost - current_retreat.price) *
                    Decimal(TAX_RATE + 1),
                    2
                ),
            }

            Order.send_invoice([order.user.email], merge_data)

        # Send refund confirmation email
        if need_refund:
            merge_data = {
                'DATETIME': timezone.localtime().strftime("%x %X"),
                'ORDER_ID': order_line.order.id,
                'CUSTOMER_NAME':
                user.first_name + " " + user.last_name,
                'CUSTOMER_EMAIL': user.email,
                'CUSTOMER_NUMBER': user.id,
                'TYPE': "Remboursement",
                'NEW_RETREAT': new_retreat,
                'OLD_RETREAT': old_retreat,
                'SUBTOTAL':
                old_retreat.price - new_retreat.price,
                'COST': round(amount/100, 2),
                'TAX': round(Decimal(tax), 2),
            }

            plain_msg = render_to_string("refund.txt", merge_data)
            msg_html = render_to_string("refund.html", merge_data)

            try:
                send_mail(
                    "Confirmation de remboursement",
                    plain_msg,
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    html_message=msg_html,
                )
            except Exception as err:
                additional_data = {
                    'title': "Confirmation de remboursement",
                    'default_from': settings.DEFAULT_FROM_EMAIL,
                    'user_email': user.email,
                    'merge_data': merge_data,
                    'template': 'refund'
                }
                Log.error(
                    source='SENDING_BLUE_TEMPLATE',
                    message=err,
                    additional_data=json.dumps(additional_data)
                )
                raise

        # Send exchange confirmation email
        if validated_data.get('retreat'):
            merge_data = {
                'DATETIME': timezone.localtime().strftime("%x %X"),
                'CUSTOMER_NAME': user.first_name + " " + user.last_name,
                'CUSTOMER_EMAIL': user.email,
                'CUSTOMER_NUMBER': user.id,
                'TYPE': "Échange",
                'NEW_RETREAT': new_retreat,
                'OLD_RETREAT': old_retreat,
            }
            if len(new_retreat.pictures.all()):
                merge_data['RETREAT_PICTURE'] = "{0}{1}".format(
                    settings.MEDIA_URL,
                    new_retreat.pictures.first().picture.url
                )

            plain_msg = render_to_string("exchange.txt", merge_data)
            msg_html = render_to_string("exchange.html", merge_data)

            try:
                send_mail(
                    "Confirmation d'échange",
                    plain_msg,
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    html_message=msg_html,
                )
            except Exception as err:
                additional_data = {
                    'title': "Confirmation d'échange",
                    'default_from': settings.DEFAULT_FROM_EMAIL,
                    'user_email': user.email,
                    'merge_data': merge_data,
                    'template': 'exchange'
                }
                Log.error(
                    source='SENDING_BLUE_TEMPLATE',
                    message=err,
                    additional_data=json.dumps(additional_data)
                )
                raise

            merge_data = {
                'RETREAT': new_retreat,
                'USER': instance.user,
            }

            plain_msg = render_to_string(
                "retreat_info.txt",
                merge_data
            )
            msg_html = render_to_string(
                "retreat_info.html",
                merge_data
            )

            try:
                send_mail(
                    "Confirmation d'inscription à la retraite",
                    plain_msg,
                    settings.DEFAULT_FROM_EMAIL,
                    [instance.user.email],
                    html_message=msg_html,
                )
            except Exception as err:
                additional_data = {
                    'title': "Confirmation d'inscription à la retraite",
                    'default_from': settings.DEFAULT_FROM_EMAIL,
                    'user_email': user.email,
                    'merge_data': merge_data,
                    'template': 'retreat_info'
                }
                Log.error(
                    source='SENDING_BLUE_TEMPLATE',
                    message=err,
                    additional_data=json.dumps(additional_data)
                )
                raise

        return Reservation.objects.get(id=instance_pk)

    class Meta:
        model = Reservation
        exclude = ('deleted', )
        extra_kwargs = {
            'retreat': {
                'help_text': _("Retreat represented by the picture."),
                'view_name': 'retreat:retreat-detail',
            },
            'invitation': {
                'help_text': _("Retreat represented by the picture."),
                'view_name': 'retreat:retreatinvitation-detail',
            },
            'is_active': {
                'required': False,
                'help_text': _("Whether the reservation is active or not."),
            },
            'url': {
                'view_name': 'retreat:reservation-detail',
            },
        }


class WaitQueueSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    created_at = serializers.ReadOnlyField()
    list_size = serializers.SerializerMethodField()

    def validate_user(self, obj):
        """
        Subscribe the authenticated user.
        If the authenticated user is an admin (is_staff), use the user provided
        in the request's 'user' field.
        """
        if self.context['request'].user.is_staff:
            return obj
        return self.context['request'].user

    class Meta:
        model = WaitQueue
        fields = '__all__'
        extra_kwargs = {
            'retreat': {
                'view_name': 'retreat:retreat-detail',
            },
            'url': {
                'view_name': 'retreat:waitqueue-detail',
            },
        }

    def get_list_size(self, obj):
        return WaitQueue.objects.filter(retreat=obj.retreat).count()


class WaitQueueNotificationSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    created_at = serializers.ReadOnlyField()

    class Meta:
        model = WaitQueue
        fields = '__all__'
        extra_kwargs = {
            'retreat': {
                'view_name': 'retreat:retreat-detail',
            },
            'url': {
                'view_name': 'retreat:waitqueuenotification-detail',
            },
        }


class RetreatInvitationSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    url_token = serializers.ReadOnlyField()
    front_url = serializers.ReadOnlyField()
    nb_places_used = serializers.ReadOnlyField()
    retreat_detail = RetreatSerializer(read_only=True, source='retreat')
    coupon_detail = CouponSerializer(read_only=True, source='coupon')

    class Meta:
        model = RetreatInvitation
        fields = '__all__'
        extra_kwargs = {
            'retreat': {
                'help_text': _("Retreat"),
                'view_name': 'retreat:retreat-detail',
            },
            'url': {
                'view_name': 'retreat:retreatinvitation-detail',
            },
        }
