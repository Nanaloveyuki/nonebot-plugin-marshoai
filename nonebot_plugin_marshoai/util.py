import base64
import json
import mimetypes
import re
import ssl
import uuid
from typing import Any, Dict, List, Optional, Union

import aiofiles  # type: ignore
import httpx
import nonebot_plugin_localstore as store
from azure.ai.inference.aio import ChatCompletionsClient
from azure.ai.inference.models import AssistantMessage, SystemMessage, UserMessage
from nonebot import get_driver
from nonebot.log import logger
from nonebot_plugin_alconna import Image as ImageMsg
from nonebot_plugin_alconna import Text as TextMsg
from nonebot_plugin_alconna import UniMessage
from openai import AsyncOpenAI, AsyncStream, NotGiven
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from zhDateTime import DateTime  # type: ignore

from ._types import DeveloperMessage
from .cache.decos import *
from .config import config
from .constants import CODE_BLOCK_PATTERN, IMG_LATEX_PATTERN, OPENAI_NEW_MODELS
from .deal_latex import ConvertLatex

# nickname_json = None  # 记录昵称
# praises_json = None  # 记录夸赞名单
loaded_target_list: List[str] = []  # 记录已恢复备份的上下文的列表

NOT_GIVEN = NotGiven()

# 时间参数相关
if config.marshoai_enable_time_prompt:
    _weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    _time_prompt = "现在的时间是{date_time}{weekday_name}，{lunar_date}。"


# noinspection LongLine
_browser_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0"
}
"""
最新的火狐用户代理头
"""


# noinspection LongLine
_praises_init_data = {
    "like": [
        {
            "name": "Asankilp",
            "advantages": "赋予了Marsho猫娘人格，在vim与vscode的加持下为Marsho写了许多代码，使Marsho更加可爱",
        }
    ]
}
"""
初始夸赞名单之数据
"""
_ssl_context = ssl.create_default_context()
_ssl_context.set_ciphers("DEFAULT")


async def get_image_raw_and_type(
    url: str, timeout: int = 10
) -> Optional[tuple[bytes, str]]:
    """
    获取图片的二进制数据

    参数:
        url: str 图片链接
        timeout: int 超时时间 秒

    return:
        tuple[bytes, str]: 图片二进制数据, 图片MIME格式
    """

    async with httpx.AsyncClient(verify=_ssl_context) as client:
        response = await client.get(url, headers=_browser_headers, timeout=timeout)
        if response.status_code == 200:
            # 获取图片数据
            content_type = response.headers.get("Content-Type")
            if not content_type:
                content_type = mimetypes.guess_type(url)[0]
            # image_format = content_type.split("/")[1] if content_type else "jpeg"
            return response.content, str(content_type)
        else:
            return None


async def get_image_b64(url: str, timeout: int = 10) -> Optional[str]:
    """
    获取图片的base64编码

    参数:
        url: 图片链接
        timeout: 超时时间 秒

    return: 图片base64编码
    """

    if data_type := await get_image_raw_and_type(url, timeout):
        # image_format = content_type.split("/")[1] if content_type else "jpeg"
        base64_image = base64.b64encode(data_type[0]).decode("utf-8")
        data_url = "data:{};base64,{}".format(data_type[1], base64_image)
        return data_url
    else:
        return None


async def make_chat_openai(
    client: AsyncOpenAI,
    msg: list,
    model_name: str,
    tools: Optional[list] = None,
    stream: bool = False,
) -> Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]:
    """
    使用 Openai SDK 调用ai获取回复

    参数:
        client: 用于与AI模型进行通信
        msg: 消息内容
        model_name: 指定AI模型名
        tools: 工具列表
    """
    return await client.chat.completions.create(  # type: ignore
        messages=msg,
        model=model_name,
        tools=tools or NOT_GIVEN,
        timeout=config.marshoai_timeout,
        stream=stream,
        **config.marshoai_model_args,
    )


@from_cache("praises")
async def get_praises():
    praises_file = store.get_plugin_data_file(
        "praises.json"
    )  # 夸赞名单文件使用localstore存储
    if not praises_file.exists():
        async with aiofiles.open(praises_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(_praises_init_data, ensure_ascii=False, indent=4))
    async with aiofiles.open(praises_file, "r", encoding="utf-8") as f:
        data = json.loads(await f.read())
    praises_json = data
    return praises_json


@update_to_cache("praises")
async def refresh_praises_json():
    praises_file = store.get_plugin_data_file("praises.json")
    if not praises_file.exists():
        with open(praises_file, "w", encoding="utf-8") as f:
            json.dump(_praises_init_data, f, ensure_ascii=False, indent=4)  # 异步？
    async with aiofiles.open(praises_file, "r", encoding="utf-8") as f:
        data = json.loads(await f.read())
    return data


async def build_praises() -> str:
    praises = await get_praises()
    result = ["你喜欢以下几个人物，他们有各自的优点："]
    for item in praises["like"]:
        result.append(f"名字：{item['name']}，优点：{item['advantages']}")
    return "\n".join(result)


async def save_context_to_json(name: str, context: Any, path: str):
    (context_dir := store.get_plugin_data_dir() / path).mkdir(
        parents=True, exist_ok=True
    )
    # os.makedirs(context_dir, exist_ok=True)
    with open(context_dir / f"{name}.json", "w", encoding="utf-8") as json_file:
        json.dump(context, json_file, ensure_ascii=False, indent=4)


async def load_context_from_json(name: str, path: str) -> list:
    """从指定路径加载历史记录"""
    (context_dir := store.get_plugin_data_dir() / path).mkdir(
        parents=True, exist_ok=True
    )
    if (file_path := context_dir / f"{name}.json").exists():
        async with aiofiles.open(file_path, "r", encoding="utf-8") as json_file:
            return json.loads(await json_file.read())
    else:
        return []


@from_cache("nickname")
async def get_nicknames():
    """获取nickname_json, 优先来源于缓存"""
    filename = store.get_plugin_data_file("nickname.json")
    # noinspection PyBroadException
    try:
        async with aiofiles.open(filename, "r", encoding="utf-8") as f:
            nickname_json = json.loads(await f.read())
    except (json.JSONDecodeError, FileNotFoundError):
        nickname_json = {}
    return nickname_json


@update_to_cache("nickname")
async def set_nickname(user_id: str, name: str):
    filename = store.get_plugin_data_file("nickname.json")
    if not filename.exists():
        data = {}
    else:
        async with aiofiles.open(filename, "r", encoding="utf-8") as f:
            data = json.loads(await f.read())
    data[user_id] = name
    if name == "" and user_id in data:
        del data[user_id]
    async with aiofiles.open(filename, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=4))
    return data


async def get_nickname_by_user_id(user_id: str):
    nickname_json = await get_nicknames()
    return nickname_json.get(user_id, "")


@update_to_cache("nickname")
async def refresh_nickname_json():
    """强制刷新nickname_json"""
    # noinspection PyBroadException
    try:
        async with aiofiles.open(
            store.get_plugin_data_file("nickname.json"), "r", encoding="utf-8"
        ) as f:
            nickname_json = json.loads(await f.read())
        return nickname_json
    except (json.JSONDecodeError, FileNotFoundError):
        logger.error("刷新 nickname_json 表错误：无法载入 nickname.json 文件")


async def get_prompt(model: str) -> List[Dict[str, Any]]:
    """获取系统提示词"""
    prompts = config.marshoai_additional_prompt
    if config.marshoai_enable_praises:
        praises_prompt = await build_praises()
        prompts += praises_prompt

    if config.marshoai_enable_time_prompt:
        prompts += _time_prompt.format(
            date_time=(current_time := DateTime.now()).strftime(
                "%Y年%m月%d日 %H:%M:%S"
            ),
            weekday_name=_weekdays[current_time.weekday()],
            lunar_date=current_time.chinesize.date_hanzify(
                "农历{干支年}{生肖}年{月份}月{数序日}"
            ),
        )

    marsho_prompt = config.marshoai_prompt
    sysprompt_content = marsho_prompt + prompts
    prompt_list: List[Dict[str, Any]] = []
    if not config.marshoai_enable_sysasuser_prompt:
        if model not in OPENAI_NEW_MODELS:
            prompt_list += [SystemMessage(content=sysprompt_content).as_dict()]
        else:
            prompt_list += [DeveloperMessage(content=sysprompt_content).as_dict()]
    else:
        prompt_list += [UserMessage(content=sysprompt_content).as_dict()]
        prompt_list += [
            AssistantMessage(content=config.marshoai_sysasuser_prompt).as_dict()
        ]
    return prompt_list


def suggest_solution(errinfo: str) -> str:
    # noinspection LongLine
    suggestions = {
        "content_filter": "消息已被内容过滤器过滤。请调整聊天内容后重试。",
        "RateLimitReached": "模型达到调用速率限制。请稍等一段时间或联系Bot管理员。",
        "tokens_limit_reached": "请求token达到上限。请重置上下文。",
        "content_length_limit": "请求体过大。请重置上下文。",
        "unauthorized": "访问token无效。请联系Bot管理员。",
        "invalid type: parameter messages.content is of type array but should be of type string.": "聊天请求体包含此模型不支持的数据类型。请重置上下文。",
        "At most 1 image(s) may be provided in one request.": "此模型只能在上下文中包含1张图片。如果此前的聊天已经发送过图片，请重置上下文。",
    }

    for key, suggestion in suggestions.items():
        if key in errinfo:
            return f"\n{suggestion}"

    return ""


async def get_backup_context(target_id: str, target_private: bool) -> list:
    """获取历史上下文"""
    global loaded_target_list
    if target_private:
        target_uid = f"private_{target_id}"
    else:
        target_uid = f"group_{target_id}"
    if target_uid not in loaded_target_list:
        loaded_target_list.append(target_uid)
        return await load_context_from_json(
            f"back_up_context_{target_uid}", "contexts/backup"
        )
    return []


"""
以下函数依照 Mulan PSL v2 协议授权

函数: parse_markdown, get_uuid_back2codeblock

版权所有 © 2024 金羿ELS
Copyright (R) 2024 Eilles(EillesWan@outlook.com)

Licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details.
"""

if config.marshoai_enable_richtext_parse:

    latex_convert = ConvertLatex()  # 开启一个转换实例

    @get_driver().on_bot_connect
    async def load_latex_convert():
        await latex_convert.load_channel(None)

    async def get_uuid_back2codeblock(
        msg: str, code_blank_uuid_map: list[tuple[str, str]]
    ):

        for torep, rep in code_blank_uuid_map:
            msg = msg.replace(torep, rep)

        return msg

    async def parse_richtext(msg: str) -> UniMessage:
        """
        人工智能给出的回答一般不会包含 HTML 嵌入其中，但是包含图片或者 LaTeX 公式、代码块，都很正常。
        这个函数会把这些都以图片形式嵌入消息体。
        """

        if not IMG_LATEX_PATTERN.search(msg):  # 没有图片和LaTeX标签
            return UniMessage(msg)

        result_msg = UniMessage()  # type: ignore
        code_blank_uuid_map = [
            (uuid.uuid4().hex, cbp.group()) for cbp in CODE_BLOCK_PATTERN.finditer(msg)
        ]

        last_tag_index = 0

        # 代码块渲染麻烦，先不处理
        for rep, torep in code_blank_uuid_map:
            msg = msg.replace(torep, rep)

        # for to_rep in CODE_SINGLE_PATTERN.finditer(msg):
        #     code_blank_uuid_map.append((rep := uuid.uuid4().hex, to_rep.group()))
        #     msg = msg.replace(to_rep.group(), rep)

        # print("#####################\n", msg, "\n\n")

        # 插入图片
        for each_find_tag in IMG_LATEX_PATTERN.finditer(msg):

            tag_found = await get_uuid_back2codeblock(
                each_find_tag.group(), code_blank_uuid_map
            )
            result_msg.append(
                TextMsg(
                    await get_uuid_back2codeblock(
                        msg[last_tag_index : msg.find(tag_found)], code_blank_uuid_map
                    )
                )
            )

            last_tag_index = msg.find(tag_found) + len(tag_found)

            if each_find_tag.group(1):

                # 图形一定要优先考虑
                # 别忘了有些图形的地址就是 LaTeX，所以要优先判断

                image_description = tag_found[2 : tag_found.find("]")]
                image_url = tag_found[tag_found.find("(") + 1 : -1]

                if image_ := await get_image_raw_and_type(image_url):

                    result_msg.append(
                        ImageMsg(
                            raw=image_[0],
                            mimetype=image_[1],
                            name=image_description + ".png",
                        )
                    )
                    result_msg.append(TextMsg("（{}）".format(image_description)))

                else:
                    result_msg.append(TextMsg(tag_found))
            elif each_find_tag.group(2):

                latex_exp = await get_uuid_back2codeblock(
                    each_find_tag.group()
                    .replace("$", "")
                    .replace("\\(", "")
                    .replace("\\)", "")
                    .replace("\\[", "")
                    .replace("\\]", ""),
                    code_blank_uuid_map,
                )
                latex_generate_ok, latex_generate_result = (
                    await latex_convert.generate_png(
                        latex_exp,
                        dpi=300,
                        foreground_colour=config.marshoai_main_colour,
                    )
                )

                if latex_generate_ok:
                    result_msg.append(
                        ImageMsg(
                            raw=latex_generate_result,  # type: ignore
                            mimetype="image/png",
                            name="latex.png",
                        )
                    )
                else:
                    result_msg.append(TextMsg(latex_exp + "（公式解析失败）"))
                    if isinstance(latex_generate_result, str):
                        result_msg.append(TextMsg(latex_generate_result))
                    else:
                        result_msg.append(
                            ImageMsg(
                                raw=latex_generate_result,
                                mimetype="image/png",
                                name="latex_error.png",
                            )
                        )
            else:
                result_msg.append(TextMsg(tag_found + "（未知内容解析失败）"))

        result_msg.append(
            TextMsg(
                await get_uuid_back2codeblock(msg[last_tag_index:], code_blank_uuid_map)
            )
        )

        return result_msg


"""
Mulan PSL v2 协议授权部分结束
"""


def extract_content_and_think(
    message: ChatCompletionMessage,
) -> tuple[str, str | None, ChatCompletionMessage]:
    """
    处理 API 返回的消息对象，提取其中的内容和思维链，并返回处理后的消息，思维链，消息对象。

    Args:
        message (ChatCompletionMessage): API 返回的消息对象。
    Returns:

        - content (str): 提取出的消息内容。

        - thinking (str | None): 提取出的思维链，如果没有则为 None。

        - message (ChatCompletionMessage): 移除了思维链的消息对象。

    本函数参考自 [nonebot-plugin-deepseek](https://github.com/KomoriDev/nonebot-plugin-deepseek)
    """
    try:
        thinking = message.reasoning_content  # type: ignore
    except AttributeError:
        thinking = None
    if thinking:
        delattr(message, "reasoning_content")
    else:
        think_blocks = re.findall(
            r"<think>(.*?)</think>", message.content or "", flags=re.DOTALL
        )
        thinking = "\n".join([block.strip() for block in think_blocks if block.strip()])

    content = re.sub(
        r"<think>.*?</think>", "", message.content or "", flags=re.DOTALL
    ).strip()
    message.content = content

    return content, thinking, message
