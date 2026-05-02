# -*- coding: utf-8 -*-
"""微信公告 Markdown 与 macOS 剪贴板工具"""
import markdown


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
    将 HTML 内容写入 macOS 剪贴板。

    Args:
        html: HTML 内容字符串

    Returns:
        成功时返回 True
    """
    from ..platform import platform

    return platform.clipboard.set_html(html)


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
