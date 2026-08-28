#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.constants
======================

OneBot v11 协议常量集中地。

任何协议字符串字面量（如事件类型、消息段类型、API action）都必须
从此处引用，禁止散落在其它文件中。
"""

# ============================================================
# 事件类型
# ============================================================

OB_POST_MESSAGE = "message"          # 收到消息
OB_POST_MESSAGE_SENT = "message_sent"  # 自己发出去的消息（NapCat 默认关闭）
OB_POST_NOTICE = "notice"            # 通知（群成员变动、戳一戳等）
OB_POST_REQUEST = "request"          # 请求（加好友、加群等）
OB_POST_META = "meta_event"          # 元事件（心跳、生命周期）

# meta_event_type
OB_META_LIFECYCLE = "lifecycle"
OB_META_HEARTBEAT = "heartbeat"

# notice_type
OB_NOTICE_GROUP_UPLOAD = "group_upload"
OB_NOTICE_GROUP_ADMIN = "group_admin"
OB_NOTICE_GROUP_DECREASE = "group_decrease"
OB_NOTICE_GROUP_INCREASE = "group_increase"
OB_NOTICE_GROUP_BAN = "group_ban"
OB_NOTICE_FRIEND_ADD = "friend_add"
OB_NOTICE_GROUP_RECALL = "group_recall"
OB_NOTICE_FRIEND_RECALL = "friend_recall"
OB_NOTICE_NOTIFY = "notify"          # poke / lucky_king / honor 等
OB_NOTICE_GROUP_CARD = "group_card"

# request_type
OB_REQUEST_FRIEND = "friend"
OB_REQUEST_GROUP = "group"

# message_type
OB_MSG_PRIVATE = "private"
OB_MSG_GROUP = "group"

# sub_type
OB_SUB_NORMAL = "normal"
OB_SUB_FRIEND = "friend"
OB_SUB_GROUP = "group"
OB_SUB_OTHER = "other"


# ============================================================
# 消息段类型
# ============================================================

OB_SEG_TEXT = "text"
OB_SEG_FACE = "face"
OB_SEG_IMAGE = "image"
OB_SEG_RECORD = "record"      # 语音
OB_SEG_VIDEO = "video"
OB_SEG_AT = "at"
OB_SEG_REPLY = "reply"
OB_SEG_FORWARD = "forward"
OB_SEG_FILE = "file"
OB_SEG_JSON = "json"
OB_SEG_XML = "xml"
OB_SEG_POKE = "poke"
OB_SEG_SHARE = "share"
OB_SEG_CONTACT = "contact"
OB_SEG_LOCATION = "location"
OB_SEG_MUSIC = "music"


# ============================================================
# API actions
# ============================================================

OB_API_SEND_PRIVATE_MSG = "send_private_msg"
OB_API_SEND_GROUP_MSG = "send_group_msg"
OB_API_SEND_MSG = "send_msg"            # 通用发送（OneBot 11 新增）
OB_API_DELETE_MSG = "delete_msg"
OB_API_GET_MSG = "get_msg"
OB_API_GET_LOGIN_INFO = "get_login_info"
OB_API_GET_FRIEND_LIST = "get_friend_list"
OB_API_GET_GROUP_LIST = "get_group_list"


# ============================================================
# 状态码
# ============================================================

OB_RETCODE_OK = 0
OB_RETCODE_ASYNC = 1        # 异步处理中
OB_RETCODE_FAILED = -1
