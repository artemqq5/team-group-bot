from enum import Enum


class TypeOfAdmins(Enum):
    ADMIN = "ADMIN"
    ADMIN_AGENCY = "ADMIN_AGENCY"
    ADMIN_APPS = "ADMIN_APPS"
    ADMIN_SHOP = "ADMIN_SHOP"
    ADMIN_CREO = "ADMIN_CREO"
    ADMIN_MEDIA = "ADMIN_MEDIA"
    ADMIN_MP = "ADMIN_MP"
    ADMIN_MT = "ADMIN_MT"


class TypeOfChats(Enum):
    AGENCY_FB = "agency_fb"
    AGENCY_GOOGLE = "agency_google"
    APPS = "apps"
    SHOP_GOOGLE = "shop_google"
    SHOP_FB = "shop_fb"
    CONSOLE = "console"
    CREO = "creo"
    AFFILIATE_MP = "affiliate_mp"
    PARTNER_MP = "partner_mp"
    MEDIA_MT = "media_mt"
    MEDIA_MP = "media_mp"
    PARTNER_MT = "partner_mt"
    ALL = "all"


def has_value_enum(enum, value):
    return value in (item.value for item in enum)


access_admin_to_chat = {
    TypeOfAdmins.ADMIN.value: [
        TypeOfChats.AGENCY_FB.value,
        TypeOfChats.AGENCY_GOOGLE.value,
        TypeOfChats.APPS.value,
        TypeOfChats.SHOP_GOOGLE.value,
        TypeOfChats.SHOP_FB.value,
        TypeOfChats.CONSOLE.value,
        TypeOfChats.CREO.value,
        TypeOfChats.AFFILIATE_MP.value,
        TypeOfChats.PARTNER_MP.value,
        TypeOfChats.MEDIA_MT.value,
        TypeOfChats.MEDIA_MP.value,
        TypeOfChats.PARTNER_MT.value,
        TypeOfChats.ALL.value,
    ],

    TypeOfAdmins.ADMIN_AGENCY.value: [TypeOfChats.AGENCY_FB.value, TypeOfChats.AGENCY_GOOGLE.value],

    TypeOfAdmins.ADMIN_APPS.value: [TypeOfChats.APPS.value],

    TypeOfAdmins.ADMIN_SHOP.value: [
        TypeOfChats.SHOP_GOOGLE.value,
        TypeOfChats.SHOP_FB.value,
        TypeOfChats.CONSOLE.value
    ],

    TypeOfAdmins.ADMIN_CREO.value: [TypeOfChats.CREO.value],

    TypeOfAdmins.ADMIN_MEDIA.value: [TypeOfChats.MEDIA_MT.value, TypeOfChats.MEDIA_MP.value],

    TypeOfAdmins.ADMIN_MP.value: [TypeOfChats.AFFILIATE_MP.value, TypeOfChats.PARTNER_MP.value],

    TypeOfAdmins.ADMIN_MT.value: [TypeOfChats.PARTNER_MT.value],
}


# Ролі, яким дозволено керувати MCP-ключами та читати журнал
MCP_KEY_ROLES = (TypeOfAdmins.ADMIN.value,)
