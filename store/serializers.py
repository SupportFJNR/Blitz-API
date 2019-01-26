from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from decimal import Decimal
import random
import string
import uuid

from django.apps import apps
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction, models
from django.db.models import Q
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from blitz_api.services import (remove_translation_fields,
                                check_if_translated_field,)
from workplace.models import Reservation
from retirement.models import Reservation as RetirementReservation
from retirement.models import WaitQueueNotification, Retirement

from .exceptions import PaymentAPIError
from .models import (Package, Membership, Order, OrderLine, BaseProduct,
                     PaymentProfile, CustomPayment, Coupon, CouponUser, Refund,
                     )
from .services import (charge_payment,
                       create_external_payment_profile,
                       create_external_card,
                       get_external_cards,
                       PAYSAFE_CARD_TYPE,
                       validate_coupon_for_order, )

User = get_user_model()

TAX_RATE = settings.LOCAL_SETTINGS['SELLING_TAX']


class BaseProductSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    order_lines = serializers.HyperlinkedRelatedField(
        many=True,
        read_only=True,
        view_name='orderline-detail'
    )
    price = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        min_value=0.1,
    )
    available = serializers.BooleanField(
        required=True,
    )
    name = serializers.CharField(
        required=False,
    )
    name_fr = serializers.CharField(
        required=False,
        allow_null=True,
    )
    name_en = serializers.CharField(
        required=False,
        allow_null=True,
    )

    def to_representation(self, instance):
        user = self.context['request'].user
        data = super(BaseProductSerializer, self).to_representation(instance)
        if not user.is_staff:
            data.pop("order_lines")
            data = remove_translation_fields(data)
        return data

    class Meta:
        model = BaseProduct
        fields = '__all__'
        abstract = True


class MembershipSerializer(BaseProductSerializer):

    def validate(self, attr):
        action = self.context['request'].parser_context['view'].action
        if action != 'partial_update':
            if not check_if_translated_field('name', attr):
                raise serializers.ValidationError({
                    'name': _("This field is required.")
                })
        return super(MembershipSerializer, self).validate(attr)

    class Meta:
        model = Membership
        fields = '__all__'
        extra_kwargs = {
            'name': {
                'help_text': _("Name of the membership."),
                'validators': [
                    UniqueValidator(queryset=Membership.objects.all())
                ],
            },
        }


class PackageSerializer(BaseProductSerializer):
    reservations = serializers.IntegerField(
        min_value=1,
    )

    def validate(self, attr):
        action = self.context['request'].parser_context['view'].action
        if action != 'partial_update':
            if not check_if_translated_field('name', attr):
                raise serializers.ValidationError({
                    'name': _("This field is required.")
                })
        return super(PackageSerializer, self).validate(attr)

    class Meta:
        model = Package
        fields = '__all__'
        extra_kwargs = {
            'name': {
                'help_text': _("Name of the package."),
                'validators': [
                    UniqueValidator(queryset=Package.objects.all())
                ],
            },
        }


class CustomPaymentSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    authorization_id = serializers.ReadOnlyField()
    settlement_id = serializers.ReadOnlyField()
    single_use_token = serializers.CharField(
        write_only=True,
        required=True,
    )

    def create(self, validated_data):
        """
        Create a custom payment and charge the user.
        """
        user = validated_data['user']
        single_use_token = validated_data.pop('single_use_token')
        # Temporary IDs until the external profile is created.
        validated_data['authorization_id'] = "0"
        validated_data['settlement_id'] = "0"
        validated_data['transaction_date'] = timezone.now()

        with transaction.atomic():
            custom_payment = CustomPayment.objects.create(**validated_data)
            amount = int(round(custom_payment.price*100))

            # Charge the order with the external payment API
            try:
                charge_response = charge_payment(
                    amount,
                    single_use_token,
                    str(custom_payment.id)
                )
            except PaymentAPIError as err:
                raise serializers.ValidationError({
                    'non_field_errors': [err]
                })

            charge_res_content = charge_response.json()
            custom_payment.authorization_id = charge_res_content['id']
            custom_payment.settlement_id = charge_res_content[
                'settlements'
            ][0]['id']
            custom_payment.reference_number = charge_res_content[
                'merchantRefNum'
            ]
            custom_payment.save()

            # TAX_RATE = settings.LOCAL_SETTINGS['SELLING_TAX']

            items = [
                {
                    'price': custom_payment.price,
                    'name': custom_payment.name,
                }
            ]

            # Send custom_payment confirmation email
            merge_data = {
                'STATUS': "APPROUVÉE",
                'CARD_NUMBER': charge_res_content['card']['lastDigits'],
                'CARD_TYPE': PAYSAFE_CARD_TYPE[
                    charge_res_content['card']['type']
                ],
                'DATETIME': timezone.localtime().strftime("%x %X"),
                'ORDER_ID': custom_payment.id,
                'CUSTOMER_NAME': user.first_name + " " + user.last_name,
                'CUSTOMER_EMAIL': user.email,
                'CUSTOMER_NUMBER': user.id,
                'AUTHORIZATION': custom_payment.authorization_id,
                'TYPE': "Achat",
                'ITEM_LIST': items,
                # No tax applied on custom payments.
                'TAX': "0.00",
                'COST': custom_payment.price,
            }

            plain_msg = render_to_string("invoice.txt", merge_data)
            msg_html = render_to_string("invoice.html", merge_data)

            send_mail(
                "Confirmation d'achat",
                plain_msg,
                settings.DEFAULT_FROM_EMAIL,
                [custom_payment.user.email],
                html_message=msg_html,
            )

            user.save()

            return custom_payment

    class Meta:
        model = CustomPayment
        fields = '__all__'
        extra_kwargs = {
            'name': {
                'help_text': _("Name of the product."),
            },
            'transaction_date': {
                'read_only': True,
            },
        }


class PaymentProfileSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    cards = serializers.SerializerMethodField()

    def get_cards(self, obj):
        return get_external_cards(obj.external_api_id)

    class Meta:
        model = PaymentProfile
        fields = (
            'id',
            'name',
            'owner',
            'cards',
        )
        extra_kwargs = {
            'name': {
                'help_text': _("Name of the payment profile."),
                'validators': [
                    UniqueValidator(queryset=PaymentProfile.objects.all())
                ],
            },
        }


class OrderLineSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    content_type = serializers.SlugRelatedField(
        queryset=ContentType.objects.all(),
        slug_field='model',
    )
    coupon_real_value = serializers.ReadOnlyField()
    cost = serializers.ReadOnlyField()
    coupon = serializers.SlugRelatedField(
        slug_field='code',
        allow_null=True,
        required=False,
        read_only=True,
    )

    def validate(self, attrs):
        """Limits packages according to request user membership"""
        validated_data = super().validate(attrs)

        user = self.context['request'].user

        user_membership = user.membership
        user_academic_level = user.academic_level

        content_type = validated_data.get(
            'content_type',
            getattr(self.instance, 'content_type', None)
        )
        object_id = validated_data.get(
            'object_id',
            getattr(self.instance, 'object_id', None)
        )
        try:
            obj = content_type.get_object_for_this_type(pk=object_id)
        except content_type.model_class().DoesNotExist:
            raise serializers.ValidationError({
                'object_id': [
                    _("The referenced object does not exist.")
                ],
            })

        if (not user.is_staff
                and (content_type.model == 'package'
                     or content_type.model == 'retirement')
                and obj.exclusive_memberships.all()
                and user_membership not in obj.exclusive_memberships.all()):
            raise serializers.ValidationError({
                'object_id': [
                    _(
                        "User does not have the required membership to order "
                        "this package."
                    )
                ],
            })
        if (not user.is_staff and
                content_type.model == 'membership' and
                obj.academic_levels.all() and
                user_academic_level not in obj.academic_levels.all()):
            raise serializers.ValidationError({
                'object_id': [
                    _(
                        "User does not have the required academic_level to "
                        "order this membership."
                    )
                ],
            })

        if (content_type.model == 'membership'
                or content_type.model == 'package'
                or content_type.model == 'retirement'):
            attrs['cost'] = obj.price * validated_data.get('quantity')

        return attrs

    class Meta:
        model = OrderLine
        fields = '__all__'


class OrderLineSerializerNoOrder(OrderLineSerializer):
    class Meta:
        model = OrderLine
        fields = '__all__'
        extra_kwargs = {
            'order': {
                'read_only': True,
            },
        }


class OrderSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    authorization_id = serializers.ReadOnlyField()
    settlement_id = serializers.ReadOnlyField()
    order_lines = OrderLineSerializerNoOrder(many=True)
    payment_token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True
    )
    single_use_token = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    coupon = serializers.SlugRelatedField(
        slug_field='code',
        queryset=Coupon.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )
    # target_user = serializers.HyperlinkedRelatedField(
    #     many=False,
    #     write_only=True,
    #     view_name='user-detail',
    #     required=False,
    #     allow_null=True,
    #     queryset=User.objects.all(),
    # )
    # save_card = serializers.NullBooleanField(
    #     write_only=True,
    #     required=False,
    # )

    def create(self, validated_data):
        """
        Create an Order and charge the user.
        """
        user = self.context['request'].user
        # if validated_data.get('target_user', None):
        #     if user.is_staff:
        #         user = validated_data.pop('target_user')
        #     else:
        #         raise serializers.ValidationError({
        #             'non_field_errors': [_(
        #                "You cannot create an order for another user without "
        #                 "admin rights."
        #             )]
        #         })
        orderlines_data = validated_data.pop('order_lines')
        payment_token = validated_data.pop('payment_token', None)
        single_use_token = validated_data.pop('single_use_token', None)
        # Temporary IDs until the external profile is created.
        validated_data['authorization_id'] = "0"
        validated_data['settlement_id'] = "0"
        validated_data['reference_number'] = "0"
        validated_data['transaction_date'] = timezone.now()
        validated_data['user'] = user
        profile = PaymentProfile.objects.filter(owner=user).first()

        retirement_reservations = list()

        if single_use_token and not profile:
            # Create external profile
            try:
                create_profile_response = create_external_payment_profile(
                    user
                )
            except PaymentAPIError as err:
                raise serializers.ValidationError({
                    'non_field_errors': [err]
                })
            # Create local profile
            profile = PaymentProfile.objects.create(
                name="Paysafe",
                owner=user,
                external_api_id=create_profile_response.json()['id'],
                external_api_url='{0}{1}'.format(
                    create_profile_response.url,
                    create_profile_response.json()['id']
                )
            )

        with transaction.atomic():
            coupon = validated_data.pop('coupon', None)
            order = Order.objects.create(**validated_data)
            charge_response = None
            discount_amount = 0
            for orderline_data in orderlines_data:
                OrderLine.objects.create(order=order, **orderline_data)

            if coupon:
                coupon_info = validate_coupon_for_order(coupon, order)
                if coupon_info['valid_use']:
                    coupon_user = CouponUser.objects.get(
                        user=user,
                        coupon=coupon,
                    )
                    coupon_user.uses = coupon_user.uses + 1
                    coupon_user.save()
                    discount_amount = coupon_info['value']
                    orderline_cost = coupon_info['orderline'].cost
                    coupon_info['orderline'].cost = (
                        orderline_cost -
                        discount_amount
                    )
                    coupon_info['orderline'].coupon = coupon
                    coupon_info['orderline'].coupon_real_value = coupon_info[
                        'value'
                    ]
                    coupon_info['orderline'].save()
                else:
                    raise serializers.ValidationError(coupon_info['error'])

            amount = order.total_cost
            tax = amount * Decimal(repr(TAX_RATE))
            tax = tax.quantize(Decimal('0.01'))
            amount *= Decimal(repr(TAX_RATE + 1))
            amount = round(amount * 100, 2)

            membership_orderlines = order.order_lines.filter(
                content_type__model="membership"
            )
            package_orderlines = order.order_lines.filter(
                content_type__model="package"
            )
            reservation_orderlines = order.order_lines.filter(
                content_type__model="timeslot"
            )
            retirement_orderlines = order.order_lines.filter(
                content_type__model="retirement"
            )
            need_transaction = False

            if membership_orderlines:
                need_transaction = True
                today = timezone.now().date()
                if user.membership and user.membership_end > today:
                    raise serializers.ValidationError({
                        'non_field_errors': [_(
                            "You already have an active membership."
                        )]
                    })
                user.membership = membership_orderlines[0].content_object
                user.membership_end = (
                    timezone.now().date() + user.membership.duration
                )
            if package_orderlines:
                need_transaction = True
                for package_orderline in package_orderlines:
                    user.tickets += (
                        package_orderline.content_object.reservations *
                        package_orderline.quantity
                    )
            if reservation_orderlines:
                for reservation_orderline in reservation_orderlines:
                    timeslot = reservation_orderline.content_object
                    reserved = (
                        timeslot.reservations.filter(is_active=True).count()
                    )
                    if timeslot.price > user.tickets:
                        raise serializers.ValidationError({
                            'non_field_errors': [_(
                                "You don't have enough tickets to make this "
                                "reservation."
                            )]
                        })
                    if (timeslot.period.workplace and
                            timeslot.period.workplace.seats - reserved > 0):
                        Reservation.objects.create(
                            user=user,
                            timeslot=timeslot,
                            is_active=True
                        )
                        # Decrement user tickets for each reservation.
                        # OrderLine's quantity and TimeSlot's price will be
                        # used in the future if we want to allow multiple
                        # reservations of the same timeslot.
                        user.tickets -= 1
                    else:
                        raise serializers.ValidationError({
                            'non_field_errors': [_(
                                "There are no places left in the requested "
                                "timeslot."
                            )]
                        })
            if retirement_orderlines:
                need_transaction = True
                if not (user.phone and user.city):
                    raise serializers.ValidationError({
                        'non_field_errors': [_(
                            "Incomplete user profile. 'phone' and 'city' "
                            "field must be filled in the user profile to book "
                            "a retirement."
                        )]
                    })

                for retirement_orderline in retirement_orderlines:
                    retirement = retirement_orderline.content_object
                    user_waiting = retirement.wait_queue.filter(user=user)
                    reserved = (
                        retirement.reservations.filter(is_active=True).count()
                    )
                    if (((retirement.seats - retirement.total_reservations -
                          retirement.reserved_seats) > 0)
                            or (retirement.reserved_seats
                                and WaitQueueNotification.objects.filter(
                                    user=user, retirement=retirement))):
                        retirement_reservations.append(
                            RetirementReservation.objects.create(
                                user=user,
                                retirement=retirement,
                                order_line=retirement_orderline,
                                is_active=True
                            )
                        )
                    else:
                        raise serializers.ValidationError({
                            'non_field_errors': [_(
                                "There are no places left in the requested "
                                "retirement."
                            )]
                        })
                    if user_waiting:
                        user_waiting.delete()

            if need_transaction and payment_token and int(amount):
                # Charge the order with the external payment API
                try:
                    charge_response = charge_payment(
                        int(round(amount)),
                        payment_token,
                        str(order.id)
                    )
                except PaymentAPIError as err:
                    raise serializers.ValidationError({
                        'non_field_errors': [err]
                    })

            elif need_transaction and single_use_token and int(amount):
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
                        'non_field_errors': [err]
                    })
            elif (membership_orderlines
                  or package_orderlines
                  or retirement_orderlines) and int(amount):
                raise serializers.ValidationError({
                    'non_field_errors': [_(
                        "A payment_token or single_use_token is required to "
                        "create an order."
                    )]
                })

            if need_transaction:
                if charge_response:
                    charge_res_content = charge_response.json()
                    order.authorization_id = charge_res_content['id']
                    order.settlement_id = charge_res_content['settlements'][0][
                        'id'
                    ]
                    order.reference_number = charge_res_content[
                        'merchantRefNum'
                    ]
                else:
                    charge_res_content = {
                        'card': {
                            'lastDigits': None,
                            'type': "NONE"
                        }
                    }
                    order.authorization_id = 0
                    order.settlement_id = 0
                    order.reference_number = "charge-" + str(uuid.uuid4())
                order.save()

                orderlines = order.order_lines.filter(
                    models.Q(content_type__model='membership') |
                    models.Q(content_type__model='package') |
                    models.Q(content_type__model='retirement')
                )

                # Here, the 'details' key is used to provide details of the
                #  item to the email template.
                # As of now, only 'retirement' objects have the 'email_content'
                #  key that is used here. There is surely a better way to
                #  to handle that logic that will be more generic.
                items = [
                    {
                        'price': orderline.content_object.price,
                        'name': "{0}: {1}".format(
                            str(orderline.content_type),
                            orderline.content_object.name
                        ),
                        # Removed details section because it was only used
                        # for retirements. Retirements instead have another
                        # unique email containing details of the event.
                        # 'details':
                        #    orderline.content_object.email_content if hasattr(
                        #         orderline.content_object, 'email_content'
                        #     ) else ""
                    } for orderline in orderlines
                ]

                # Send order confirmation email
                merge_data = {
                    'STATUS': "APPROUVÉE",
                    'CARD_NUMBER': charge_res_content['card']['lastDigits'],
                    'CARD_TYPE': PAYSAFE_CARD_TYPE[
                        charge_res_content['card']['type']
                    ],
                    'DATETIME': timezone.localtime().strftime("%x %X"),
                    'ORDER_ID': order.id,
                    'CUSTOMER_NAME': user.first_name + " " + user.last_name,
                    'CUSTOMER_EMAIL': user.email,
                    'CUSTOMER_NUMBER': user.id,
                    'AUTHORIZATION': order.authorization_id,
                    'TYPE': "Achat",
                    'ITEM_LIST': items,
                    'TAX': tax,
                    'DISCOUNT': discount_amount,
                    'COUPON': coupon,
                    'SUBTOTAL': round(amount / 100 - tax, 2),
                    'COST': round(amount / 100, 2),
                }

                plain_msg = render_to_string("invoice.txt", merge_data)
                msg_html = render_to_string("invoice.html", merge_data)

                send_mail(
                    "Confirmation d'achat",
                    plain_msg,
                    settings.DEFAULT_FROM_EMAIL,
                    [order.user.email],
                    html_message=msg_html,
                )

            for retirement_reservation in retirement_reservations:
                # Send info email
                merge_data = {
                    'RETIREMENT': retirement_reservation.retirement,
                    'USER': user,
                }

                plain_msg = render_to_string(
                    "retirement_info.txt",
                    merge_data
                )
                msg_html = render_to_string(
                    "retirement_info.html",
                    merge_data
                )

                send_mail(
                    "Confirmation d'inscription à la retraite",
                    plain_msg,
                    settings.DEFAULT_FROM_EMAIL,
                    [retirement_reservation.user.email],
                    html_message=msg_html,
                )

            user.save()

            return order

    def update(self, instance, validated_data):
        orderlines_data = validated_data.pop('order_lines')
        order = super().update(instance, validated_data)
        for orderline_data in orderlines_data:
            OrderLine.objects.update_or_create(
                order=order,
                content_type=orderline_data.get('content_type'),
                object_id=orderline_data.get('object_id'),
                defaults=orderline_data,
            )
        return order

    class Meta:
        model = Order
        fields = '__all__'
        extra_kwargs = {
            'transaction_date': {
                'read_only': True,
            },
            'user': {
                'read_only': True,
            },
        }


class CouponSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()
    applicable_product_types = serializers.SlugRelatedField(
        queryset=ContentType.objects.all(),
        slug_field='model',
        many=True,
        required=False,
    )
    code = serializers.ReadOnlyField()
    value = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        min_value=0.0,
    )
    max_use = serializers.IntegerField(
        min_value=0
    )
    max_use_per_user = serializers.IntegerField(
        min_value=0
    )

    def create(self, validated_data):
        """
        Generate coupon's code and create the coupon.
        """
        code = ""
        n = 0
        used_code = Coupon.objects.all().values_list('code', flat=True)
        while ((not code or code in used_code) and (n < 100)):
            code = ''.join(
                random.choices(
                    string.ascii_uppercase.replace("O", "").replace("I", "") +
                    string.digits.replace("0", ""),
                    k=8))
            n += 1
        if n >= 100:
            raise serializers.ValidationError({
                'non_field_errors': [_(
                    "Can't generate new unique codes. Delete old coupons."
                )]
            })
        validated_data['code'] = code
        return super(CouponSerializer, self).create(validated_data)

    class Meta:
        model = Coupon
        exclude = ('deleted', )
        extra_kwargs = {
            'applicable_retirements': {
                'required': False,
                'view_name': 'retirement:retirement-detail',
            },
            'applicable_timeslots': {
                'required': False,
            },
            'applicable_packages': {
                'required': False,
            },
            'applicable_memberships': {
                'required': False,
            },
        }


class RefundSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.ReadOnlyField()

    class Meta:
        model = Refund
        exclude = ('deleted', )
