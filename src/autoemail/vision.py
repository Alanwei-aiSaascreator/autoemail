"""证件图片识别：用多模态 LLM 提取驾照等证件字段。

仅做结构化提取，不做 OCR 之外的任何判断；取不到的字段返回 None。
"""

from pydantic import BaseModel

from .llm import HttpxOpenAICompatibleProvider


class DriverLicenseInfo(BaseModel):
    full_name: str | None = None
    date_of_birth: str | None = None
    license_number: str | None = None
    address: str | None = None
    expiration_date: str | None = None
    sex: str | None = None
    state: str | None = None


def extract_driver_license(
    image_bytes: bytes,
    mime: str = "image/jpeg",
) -> DriverLicenseInfo | None:
    """识别美国驾照或州身份证正面图片，返回结构化字段；失败返回 None。"""
    provider = HttpxOpenAICompatibleProvider()
    result = provider.complete_vision_json(
        system_prompt="你是证件识别助手，准确提取美国证件上的文字信息，取不到的字段填 null。",
        user_prompt=(
            "这是一张美国驾照（Driver License）或州身份证（Identification Card）正面照片。"
            "提取字段：full_name(持证人英文全名)、date_of_birth(出生日期 YYYY-MM-DD)、"
            "license_number(证件号)、address(地址)、expiration_date(有效期 YYYY-MM-DD)、"
            "sex(性别)、state(签发州)。"
        ),
        images=[(mime, image_bytes)],
        schema=DriverLicenseInfo,
        task="driver_license",
    )
    if result.error is not None or result.parsed is None:
        return None
    return result.parsed


class SsnCardInfo(BaseModel):
    full_name: str | None = None
    ssn: str | None = None


def extract_ssn_card(
    image_bytes: bytes,
    mime: str = "image/png",
) -> SsnCardInfo | None:
    """识别美国社会安全卡（SSN Card）图片，返回姓名与社会安全号；失败返回 None。"""
    provider = HttpxOpenAICompatibleProvider()
    result = provider.complete_vision_json(
        system_prompt="你是证件识别助手，准确提取美国社会安全卡上的文字，取不到的字段填 null。",
        user_prompt=(
            "这是一张美国社会安全卡（Social Security Card）照片。提取字段："
            "full_name(持卡人姓名)、ssn(社会安全号，形如 XXX-XX-XXXX)。"
        ),
        images=[(mime, image_bytes)],
        schema=SsnCardInfo,
        task="ssn_card",
    )
    if result.error is not None or result.parsed is None:
        return None
    return result.parsed


class IdCardInfo(BaseModel):
    full_name: str | None = None
    id_number: str | None = None
    date_of_birth: str | None = None
    sex: str | None = None
    address: str | None = None
    ethnicity: str | None = None


def extract_id_card(
    image_bytes: bytes,
    mime: str = "image/png",
) -> IdCardInfo | None:
    """识别中国居民身份证正面图片，返回结构化字段；失败返回 None。"""
    provider = HttpxOpenAICompatibleProvider()
    result = provider.complete_vision_json(
        system_prompt="你是证件识别助手，准确提取中国居民身份证上的文字，取不到的字段填 null。",
        user_prompt=(
            "这是一张中国居民身份证照片。提取字段："
            "full_name(姓名)、id_number(身份证号，18位)、"
            "date_of_birth(出生日期 YYYY-MM-DD)、sex(性别)、"
            "address(住址)、ethnicity(民族)。"
        ),
        images=[(mime, image_bytes)],
        schema=IdCardInfo,
        task="id_card",
    )
    if result.error is not None or result.parsed is None:
        return None
    return result.parsed
