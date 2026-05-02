# -*- coding: utf-8 -*-
"""微信公告 Markdown 与剪贴板工具"""
import markdown
from bs4 import BeautifulSoup


def markdown_to_html(md_content: str) -> str:
    """
    将 Markdown 转换为带内联样式的 HTML。

    Args:
        md_content: Markdown 内容字符串

    Returns:
        带内联样式的 HTML 字符串
    """
    # 将 Markdown 转换为 HTML
    html_body = markdown.markdown(md_content, extensions=['tables', 'fenced_code'])

    # 添加内联样式以获得更好的渲染效果
    styled_html = html_body

    # 表格样式
    styled_html = styled_html.replace(
        '<table>',
        '<table style="border-collapse: collapse; width: 100%; margin: 10px 0;">'
    )
    styled_html = styled_html.replace(
        '<th>',
        '<th style="border: 1px solid #ddd; padding: 8px; background-color: #f5f5f5; text-align: left;">'
    )
    styled_html = styled_html.replace(
        '<td>',
        '<td style="border: 1px solid #ddd; padding: 8px;">'
    )

    # 标题样式
    styled_html = styled_html.replace(
        '<h1>',
        '<h1 style="font-size: 20px; font-weight: bold; margin: 15px 0 10px 0;">'
    )
    styled_html = styled_html.replace(
        '<h2>',
        '<h2 style="font-size: 16px; font-weight: bold; margin: 12px 0 8px 0;">'
    )
    styled_html = styled_html.replace(
        '<h3>',
        '<h3 style="font-size: 14px; font-weight: bold; margin: 10px 0 6px 0;">'
    )

    return styled_html


def copy_html_to_clipboard(html: str) -> bool:
    """
    将 HTML 以 CF_HTML 格式复制到剪贴板（Windows）。

    这允许将格式化内容粘贴到微信等应用程序中。

    Args:
        html: HTML 内容字符串

    Returns:
        成功时返回 True
    """
    # 创建包含正确头部的 CF_HTML 格式
    html_with_fragment = f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
<!--StartFragment-->
{html}
<!--EndFragment-->
</body>
</html>'''

    html_bytes = html_with_fragment.encode('utf-8')

    # 创建头部模板
    header_template = (
        "Version:0.9\r\n"
        "StartHTML:000000000\r\n"
        "EndHTML:{end_html:09d}\r\n"
        "StartFragment:000000000\r\n"
        "EndFragment:{end_fragment:09d}\r\n"
    )

    # 计算偏移量
    start_html = len(header_template.format(end_html=0, end_fragment=0).encode('utf-8'))
    end_html = start_html + len(html_bytes)

    start_fragment = html_with_fragment.find('<!--StartFragment-->')
    end_fragment = html_with_fragment.find('<!--EndFragment-->')

    if start_fragment != -1 and end_fragment != -1:
        start_fragment = start_html + start_fragment + len('<!--StartFragment-->')
        end_fragment = start_html + end_fragment

    # 创建最终头部
    header = (
        f"Version:0.9\r\n"
        f"StartHTML:{start_html:09d}\r\n"
        f"EndHTML:{end_html:09d}\r\n"
        f"StartFragment:{start_fragment:09d}\r\n"
        f"EndFragment:{end_fragment:09d}\r\n"
    )

    # 组合头部和 HTML
    cf_html = header.encode('utf-8') + html_bytes

    # 打开剪贴板并设置数据
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()

        # 注册并设置 HTML 格式
        cf_html_format = win32clipboard.RegisterClipboardFormat("HTML Format")
        win32clipboard.SetClipboardData(cf_html_format, cf_html)

        # 同时设置纯文本作为备用
        soup = BeautifulSoup(html, 'html.parser')
        plain_text = soup.get_text(separator='\n')
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, plain_text)

        return True
    finally:
        win32clipboard.CloseClipboard()


def read_markdown_file(file_path: str) -> str:
    """
    读取 Markdown 文件内容。

    Args:
        file_path: Markdown 文件路径

    Returns:
        Markdown 内容字符串
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()