import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yt_dlp.extractor.vk import VKIE

import vk_search

RAW = '{"params":[{"subs":[{"title":null,"lang":"ru","url":"x"},{"title":"a.vtt","url":"y"}]}]}'


def test_patch():
    vk_search._patch_yt_dlp_vk()
    wrapped = VKIE._parse_json
    vk_search._patch_yt_dlp_vk()
    assert VKIE._parse_json is wrapped  # no double wrap

    subs = VKIE()._parse_json(RAW, "id")["params"][0]["subs"]
    assert subs[0] == {"lang": "ru", "url": "x"}
    assert subs[1] == {"title": "a.vtt", "url": "y"}


if __name__ == "__main__":
    test_patch()
    print("ok")
