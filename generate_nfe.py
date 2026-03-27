#!/usr/bin/env python3
"""
Auto Invoice Generator - NFS-e Emissor Nacional
Automatiza a emissão mensal de NFS-e no portal nfse.gov.br/EmissorNacional

Como funciona:
  1. Você loga manualmente no portal UMA VEZ
  2. Abre o DevTools (F12) > Application > Session Storage > nfse.gov.br
  3. Copia o valor do token (ex: chave "token" ou "access_token")
  4. Cola no .env como NFSE_ACCESS_TOKEN=...
  5. Roda o script — ele injeta o token e preenche tudo sozinho

Uso:
    python generate_nfe.py                     # competência do mês atual
    python generate_nfe.py --mes 3 --ano 2026  # competência específica
    python generate_nfe.py --dry-run           # preenche mas não emite
"""

import asyncio
import argparse
import calendar
import sys
import yaml
import os
from datetime import date
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

load_dotenv()
console = Console()

MONTHS_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_descricao(config: dict, competencia: date) -> str:
    mes_nome = MONTHS_PT[competencia.month]
    ano = competencia.year
    ultimo_dia = calendar.monthrange(ano, competencia.month)[1]

    data_pgto = date(ano, competencia.month, ultimo_dia) + relativedelta(months=1)
    data_pgto = data_pgto.replace(day=config["faturamento"]["dia_pagamento"])

    valor = config["faturamento"]["valor"]
    valor_formatado = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    return config["servico"]["descricao_template"].format(
        mes=mes_nome,
        ano=ano,
        ultimo_dia=ultimo_dia,
        valor_formatado=valor_formatado,
        data_pagamento=data_pgto.strftime("%d/%m/%Y"),
    ).strip()


def print_preview(config: dict, competencia: date, descricao: str) -> None:
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    table.add_column("Campo", style="bold cyan", width=28)
    table.add_column("Valor", style="white")

    valor = config["faturamento"]["valor"]
    valor_fmt = f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    table.add_row("Competência", f"{MONTHS_PT[competencia.month]}/{competencia.year}")
    table.add_row("Emitente", config["emitente"]["nome"])
    table.add_row("CNPJ Emitente", config["emitente"]["cnpj"])
    table.add_row("Tomador", config["tomador"]["nome"])
    table.add_row("CNPJ Tomador", config["tomador"]["cnpj"])
    table.add_row("Código Tributação", config["servico"]["codigo_tributacao_nacional"])
    table.add_row("Valor", valor_fmt)
    table.add_row("Descrição", descricao)

    console.print(Panel(table, title="[bold green]NFS-e a ser emitida[/bold green]", border_style="green"))


async def injetar_token(page, token: str, config: dict) -> None:
    """
    Navega para o portal, injeta o token no sessionStorage e recarrega.
    Injeta sob todas as chaves comuns — o portal vai usar a que conhece.
    """
    url = config["portal"]["url"]
    console.print(f"[cyan]Abrindo {url}...[/cyan]")
    await page.goto(url, wait_until="domcontentloaded", timeout=config["portal"]["timeout"])

    # Chaves comuns usadas por portais React/Angular do gov.br
    chaves = config["portal"].get("token_storage_keys", [
        "token",
        "access_token",
        "authToken",
        "auth_token",
        "nfse_token",
        "TOKEN",
    ])

    js_injecao = "\n".join(
        f'sessionStorage.setItem({key!r}, {token!r});'
        for key in chaves
    )
    await page.evaluate(js_injecao)
    console.print(f"[green]✓ Token injetado no sessionStorage ({len(chaves)} chaves).[/green]")

    # Recarrega para o portal ler o token
    await page.reload(wait_until="networkidle", timeout=config["portal"]["timeout"])

    # Confirma que não foi redirecionado para login
    await asyncio.sleep(1)
    current_url = page.url
    if "login" in current_url.lower() or "sso.acesso" in current_url.lower():
        console.print(
            "[bold red]O portal redirecionou para login — token inválido ou expirado.[/bold red]\n"
            "[yellow]Faça login manualmente, copie o token do DevTools (F12 > Application > Session Storage)\n"
            "e atualize NFSE_ACCESS_TOKEN no arquivo .env[/yellow]"
        )
        await asyncio.sleep(30)  # mantém aberto para o usuário ver
        sys.exit(1)

    console.print("[green]✓ Portal autenticado com sucesso![/green]")


async def navegar_para_emitir(page, config: dict) -> None:
    console.print("[cyan]Navegando para emissão de NFS-e...[/cyan]")
    await page.wait_for_load_state("networkidle", timeout=15000)

    emitir_selectors = [
        'a:has-text("Emitir NFS-e")',
        'button:has-text("Emitir NFS-e")',
        'a:has-text("Nova NFS-e")',
        'button:has-text("Nova NFS-e")',
        'a:has-text("Emitir")',
        'button:has-text("Emitir")',
        '[data-testid="emitir-nfse"]',
    ]
    for selector in emitir_selectors:
        try:
            await page.click(selector, timeout=3000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            console.print("[green]✓ Formulário de emissão aberto.[/green]")
            return
        except PlaywrightTimeout:
            continue

    raise RuntimeError(
        "Não encontrou o botão 'Emitir NFS-e'.\n"
        "O layout do portal pode ter mudado — abra o portal manualmente e ajuste\n"
        "os seletores em navegar_para_emitir() no script."
    )


async def preencher_competencia(page, competencia: date) -> None:
    console.print(f"[cyan]Preenchendo competência: {competencia.month:02d}/{competencia.year}...[/cyan]")

    # Tenta campo único MM/AAAA
    for sel in [
        'input[name="competencia"]',
        'input[placeholder*="ompetência"]',
        'input[aria-label*="ompetência"]',
    ]:
        try:
            await page.fill(sel, f"{competencia.month:02d}/{competencia.year}", timeout=3000)
            console.print("[green]✓ Competência preenchida.[/green]")
            return
        except PlaywrightTimeout:
            continue

    # Fallback: campos separados de mês e ano
    try:
        await page.select_option('select[name="mes"], select[aria-label*="ês"]', str(competencia.month), timeout=3000)
        await page.select_option('select[name="ano"], select[aria-label*="no"]', str(competencia.year), timeout=3000)
        console.print("[green]✓ Competência preenchida (mês/ano separados).[/green]")
        return
    except PlaywrightTimeout:
        pass

    console.print("[yellow]⚠ Campo de competência não encontrado automaticamente — verifique o formulário.[/yellow]")


async def preencher_tomador(page, config: dict) -> None:
    console.print("[cyan]Preenchendo CNPJ do tomador...[/cyan]")

    cnpj = config["tomador"]["cnpj"].replace(".", "").replace("/", "").replace("-", "")

    for sel in [
        'input[name="cnpjTomador"]',
        'input[name="tomadorCnpj"]',
        'input[placeholder*="CNPJ"]',
        'input[aria-label*="CNPJ"]',
        'input[aria-label*="omador"]',
    ]:
        try:
            await page.fill(sel, cnpj, timeout=3000)
            await page.press(sel, "Tab")
            await page.wait_for_timeout(1500)  # aguarda preenchimento automático pelo portal
            console.print("[green]✓ CNPJ do tomador preenchido.[/green]")
            return
        except PlaywrightTimeout:
            continue

    console.print("[yellow]⚠ Campo de CNPJ do tomador não encontrado automaticamente.[/yellow]")


async def preencher_servico(page, config: dict, descricao: str) -> None:
    console.print("[cyan]Preenchendo dados do serviço...[/cyan]")

    codigo = config["servico"]["codigo_tributacao_nacional"]
    valor_str = f"{config['faturamento']['valor']:.2f}".replace(".", ",")

    # Código de tributação
    for sel in [
        'input[name="codigoTributacaoNacional"]',
        'input[name="codigoServico"]',
        'input[placeholder*="ributação"]',
        'input[aria-label*="ributação"]',
        'input[aria-label*="Serviço"]',
    ]:
        try:
            await page.fill(sel, codigo, timeout=3000)
            await page.press(sel, "Tab")
            await page.wait_for_timeout(1000)
            console.print("[green]✓ Código de tributação preenchido.[/green]")
            break
        except PlaywrightTimeout:
            continue

    # Descrição
    for sel in [
        'textarea[name="descricaoServico"]',
        'textarea[name="descricao"]',
        'textarea[placeholder*="escrição"]',
        'textarea[aria-label*="escrição"]',
    ]:
        try:
            await page.fill(sel, descricao, timeout=3000)
            console.print("[green]✓ Descrição preenchida.[/green]")
            break
        except PlaywrightTimeout:
            continue

    # Valor
    for sel in [
        'input[name="valorServico"]',
        'input[name="valor"]',
        'input[placeholder*="alor"]',
        'input[aria-label*="alor"]',
    ]:
        try:
            await page.fill(sel, valor_str, timeout=3000)
            console.print("[green]✓ Valor preenchido.[/green]")
            break
        except PlaywrightTimeout:
            continue


async def confirmar_e_emitir(page, dry_run: bool) -> None:
    if dry_run:
        console.print("[yellow]--dry-run ativo: formulário preenchido mas NÃO será submetido.[/yellow]")
        console.print("[yellow]O navegador ficará aberto. Pressione Ctrl+C para encerrar.[/yellow]")
        await asyncio.Event().wait()  # espera indefinidamente até Ctrl+C
        return

    console.print("[cyan]Submetendo NFS-e...[/cyan]")

    for sel in [
        'button[type="submit"]:has-text("Emitir")',
        'button:has-text("Emitir NFS-e")',
        'button:has-text("Confirmar")',
        'button:has-text("Salvar e Emitir")',
        '[data-testid="submit-nfse"]',
    ]:
        try:
            await page.click(sel, timeout=3000)
            break
        except PlaywrightTimeout:
            continue
    else:
        raise RuntimeError("Não encontrou o botão de submissão — ajuste os seletores em confirmar_e_emitir().")

    # Aguarda confirmação de sucesso
    for sel in [
        'text="NFS-e emitida com sucesso"',
        'text="Nota Fiscal emitida"',
        '[class*="success"]',
        '[class*="sucesso"]',
        '[role="alert"]',
    ]:
        try:
            await page.wait_for_selector(sel, timeout=15000)
            console.print("[bold green]✓ NFS-e emitida com sucesso![/bold green]")
            return
        except PlaywrightTimeout:
            continue

    console.print("[yellow]⚠ Submissão realizada — verifique o portal para confirmar a emissão.[/yellow]")


async def run(competencia: date, dry_run: bool, config: dict) -> None:
    token = os.getenv("NFSE_ACCESS_TOKEN", "").strip()
    if not token:
        console.print(
            "[bold red]NFSE_ACCESS_TOKEN não definido.[/bold red]\n"
            "[yellow]1. Acesse nfse.gov.br/EmissorNacional e faça login manualmente\n"
            "2. Abra DevTools (F12) > Application > Session Storage > nfse.gov.br\n"
            "3. Copie o valor do token\n"
            "4. Cole no arquivo .env como: NFSE_ACCESS_TOKEN=seu_token_aqui[/yellow]"
        )
        sys.exit(1)

    descricao = build_descricao(config, competencia)
    print_preview(config, competencia, descricao)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # sempre visível — portal gov.br precisa de navegador real
            slow_mo=config["portal"].get("slow_mo", 80),
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        try:
            await injetar_token(page, token, config)
            await navegar_para_emitir(page, config)
            await preencher_competencia(page, competencia)
            await preencher_tomador(page, config)
            await preencher_servico(page, config, descricao)
            await confirmar_e_emitir(page, dry_run)

        except KeyboardInterrupt:
            console.print("\n[yellow]Encerrado pelo usuário.[/yellow]")
        except Exception as exc:
            console.print(f"[bold red]Erro:[/bold red] {exc}")
            console.print("[yellow]Navegador ficará aberto por 2 minutos para inspeção.[/yellow]")
            await asyncio.sleep(120)
            raise
        finally:
            await browser.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Gera NFS-e mensal automaticamente no Emissor Nacional (nfse.gov.br)"
    )
    parser.add_argument("--mes", type=int, help="Mês da competência (1-12). Padrão: mês atual.")
    parser.add_argument("--ano", type=int, help="Ano da competência. Padrão: ano atual.")
    parser.add_argument("--dry-run", action="store_true", help="Preenche o formulário mas não submete.")
    return parser.parse_args()


def main():
    args = parse_args()
    today = date.today()
    mes = args.mes or today.month
    ano = args.ano or today.year

    try:
        competencia = date(ano, mes, 1)
    except ValueError:
        console.print(f"[red]Data inválida: mês={mes}, ano={ano}[/red]")
        sys.exit(1)

    config = load_config()

    console.print(
        Panel(
            f"[bold]Gerador de NFS-e[/bold]\n"
            f"Portal: {config['portal']['url']}\n"
            f"Competência: [green]{MONTHS_PT[mes]}/{ano}[/green]\n"
            f"Modo: {'[yellow]dry-run[/yellow]' if args.dry_run else '[green]emissão real[/green]'}",
            border_style="blue",
        )
    )

    asyncio.run(run(competencia, args.dry_run, config))


if __name__ == "__main__":
    main()
