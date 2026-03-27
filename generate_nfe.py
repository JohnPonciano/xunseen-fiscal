#!/usr/bin/env python3
"""
Auto Invoice Generator - NFS-e Emissor Nacional
Automatiza a emissão mensal de NFS-e no portal nfse.gov.br/EmissorNacional

Uso:
    python generate_nfe.py                  # usa a competência do mês atual
    python generate_nfe.py --mes 2 --ano 2026  # competência específica
    python generate_nfe.py --dry-run        # mostra o que seria preenchido sem emitir
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
    """Monta a descrição do serviço com o período correto."""
    mes_nome = MONTHS_PT[competencia.month]
    ano = competencia.year
    ultimo_dia = calendar.monthrange(ano, competencia.month)[1]

    data_pgto = date(ano, competencia.month, ultimo_dia) + relativedelta(months=1)
    data_pgto = data_pgto.replace(day=config["faturamento"]["dia_pagamento"])

    valor = config["faturamento"]["valor"]
    valor_formatado = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    template = config["servico"]["descricao_template"]
    return template.format(
        mes=mes_nome,
        ano=ano,
        ultimo_dia=ultimo_dia,
        valor_formatado=valor_formatado,
        data_pagamento=data_pgto.strftime("%d/%m/%Y"),
    ).strip()


def print_preview(config: dict, competencia: date, descricao: str) -> None:
    """Exibe um resumo do que será preenchido na NFS-e."""
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    table.add_column("Campo", style="bold cyan", width=28)
    table.add_column("Valor", style="white")

    mes_ano = f"{MONTHS_PT[competencia.month]}/{competencia.year}"
    valor = config["faturamento"]["valor"]

    table.add_row("Competência", mes_ano)
    table.add_row("Emitente", config["emitente"]["nome"])
    table.add_row("CNPJ Emitente", config["emitente"]["cnpj"])
    table.add_row("Tomador", config["tomador"]["nome"])
    table.add_row("CNPJ Tomador", config["tomador"]["cnpj"])
    table.add_row("Código Tributação", config["servico"]["codigo_tributacao_nacional"])
    table.add_row("Valor", f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    table.add_row("Descrição", descricao)

    console.print(Panel(table, title="[bold green]NFS-e a ser emitida[/bold green]", border_style="green"))


async def login_govbr(page, cpf: str, senha: str) -> None:
    """Realiza login no gov.br."""
    console.print("[cyan]Iniciando login no gov.br...[/cyan]")

    # Campo CPF
    await page.wait_for_selector('input[name="accountId"], input[id="accountId"], input[placeholder*="CPF"]', timeout=15000)
    cpf_limpo = cpf.replace(".", "").replace("-", "")
    await page.fill('input[name="accountId"], input[id="accountId"], input[placeholder*="CPF"]', cpf_limpo)
    await page.click('button[type="submit"], button:has-text("Continuar"), button:has-text("Próximo")')

    # Campo senha
    await page.wait_for_selector('input[type="password"]', timeout=10000)
    await page.fill('input[type="password"]', senha)
    await page.click('button[type="submit"], button:has-text("Entrar"), button:has-text("Acessar")')

    # Aguarda possível 2FA (QR code, SMS, app gov.br)
    console.print("[yellow]Se aparecer tela de verificação em 2 etapas, autorize no app gov.br ou insira o código.[/yellow]")

    # Espera o redirecionamento de volta ao Emissor Nacional
    try:
        await page.wait_for_url("**/EmissorNacional**", timeout=60000)
        console.print("[green]✓ Login realizado com sucesso![/green]")
    except PlaywrightTimeout:
        # Verifica se ainda está em tela de 2FA
        current_url = page.url
        if "sso.acesso.gov.br" in current_url or "acesso.gov.br" in current_url:
            console.print("[yellow]Aguardando autenticação adicional (2FA)... [60s][/yellow]")
            await page.wait_for_url("**/EmissorNacional**", timeout=120000)
            console.print("[green]✓ Login realizado com sucesso![/green]")
        else:
            raise


async def navegar_para_emitir(page) -> None:
    """Navega até o formulário de emissão de NFS-e."""
    console.print("[cyan]Navegando para emissão de NFS-e...[/cyan]")

    # Aguarda o dashboard carregar
    await page.wait_for_load_state("networkidle", timeout=15000)

    # Clica em "Emitir NFS-e" ou botão equivalente
    emitir_selectors = [
        'a:has-text("Emitir NFS-e")',
        'button:has-text("Emitir NFS-e")',
        'a:has-text("Nova NFS-e")',
        'button:has-text("Nova NFS-e")',
        '[data-testid="emitir-nfse"]',
    ]
    for selector in emitir_selectors:
        try:
            await page.click(selector, timeout=3000)
            break
        except PlaywrightTimeout:
            continue
    else:
        raise RuntimeError(
            "Não foi possível encontrar o botão 'Emitir NFS-e'. "
            "O layout do portal pode ter mudado — verifique o seletor em navegar_para_emitir()."
        )

    await page.wait_for_load_state("networkidle", timeout=15000)
    console.print("[green]✓ Formulário de emissão aberto.[/green]")


async def preencher_competencia(page, competencia: date) -> None:
    """Preenche o campo de competência (mês/ano)."""
    console.print(f"[cyan]Preenchendo competência: {competencia.month:02d}/{competencia.year}...[/cyan]")

    competencia_str = f"{competencia.month:02d}/{competencia.year}"

    selectors = [
        'input[name="competencia"]',
        'input[placeholder*="competência"]',
        'input[placeholder*="Competência"]',
        'input[aria-label*="competência"]',
        'input[aria-label*="Competência"]',
    ]
    for selector in selectors:
        try:
            await page.fill(selector, competencia_str, timeout=3000)
            console.print("[green]✓ Competência preenchida.[/green]")
            return
        except PlaywrightTimeout:
            continue

    # Fallback: tenta encontrar campos de mês e ano separados
    try:
        await page.select_option('select[name="mes"], select[aria-label*="mês"], select[aria-label*="Mês"]', str(competencia.month), timeout=3000)
        await page.select_option('select[name="ano"], select[aria-label*="ano"], select[aria-label*="Ano"]', str(competencia.year), timeout=3000)
        console.print("[green]✓ Competência preenchida (mês/ano separados).[/green]")
        return
    except PlaywrightTimeout:
        pass

    console.print("[yellow]⚠ Não encontrou campo de competência automaticamente. Verifique manualmente.[/yellow]")


async def preencher_tomador(page, config: dict) -> None:
    """Preenche os dados do tomador do serviço."""
    console.print("[cyan]Preenchendo dados do tomador...[/cyan]")

    cnpj = config["tomador"]["cnpj"]
    cnpj_limpo = cnpj.replace(".", "").replace("/", "").replace("-", "")

    selectors_cnpj = [
        'input[name="cnpjTomador"]',
        'input[name="tomadorCnpj"]',
        'input[placeholder*="CNPJ do tomador"]',
        'input[placeholder*="CNPJ/CPF"]',
        'input[aria-label*="CNPJ"]',
    ]
    for selector in selectors_cnpj:
        try:
            await page.fill(selector, cnpj_limpo, timeout=3000)
            await page.press(selector, "Tab")  # dispara busca automática pelo CNPJ
            await page.wait_for_timeout(1500)   # aguarda preenchimento automático
            console.print("[green]✓ CNPJ do tomador preenchido.[/green]")
            break
        except PlaywrightTimeout:
            continue

    # Preenche email se configurado
    if config["tomador"].get("email"):
        try:
            await page.fill('input[name="emailTomador"], input[placeholder*="e-mail"]', config["tomador"]["email"], timeout=3000)
        except PlaywrightTimeout:
            pass


async def preencher_servico(page, config: dict, descricao: str) -> None:
    """Preenche o bloco de serviço: código, descrição e valor."""
    console.print("[cyan]Preenchendo dados do serviço...[/cyan]")

    codigo = config["servico"]["codigo_tributacao_nacional"]
    valor = config["faturamento"]["valor"]
    valor_str = f"{valor:.2f}".replace(".", ",")

    # Código de tributação nacional
    selectors_codigo = [
        'input[name="codigoTributacaoNacional"]',
        'input[name="codigoServico"]',
        'input[placeholder*="código de tributação"]',
        'input[placeholder*="Código de Tributação"]',
        'input[aria-label*="Código de Tributação"]',
    ]
    for selector in selectors_codigo:
        try:
            await page.fill(selector, codigo, timeout=3000)
            await page.press(selector, "Tab")
            await page.wait_for_timeout(1000)
            console.print("[green]✓ Código de tributação preenchido.[/green]")
            break
        except PlaywrightTimeout:
            continue

    # Descrição do serviço
    selectors_desc = [
        'textarea[name="descricaoServico"]',
        'textarea[name="descricao"]',
        'textarea[placeholder*="descrição"]',
        'textarea[aria-label*="descrição"]',
        'textarea[aria-label*="Descrição"]',
    ]
    for selector in selectors_desc:
        try:
            await page.fill(selector, descricao, timeout=3000)
            console.print("[green]✓ Descrição preenchida.[/green]")
            break
        except PlaywrightTimeout:
            continue

    # Valor do serviço
    selectors_valor = [
        'input[name="valorServico"]',
        'input[name="valor"]',
        'input[placeholder*="valor"]',
        'input[placeholder*="Valor"]',
        'input[aria-label*="Valor"]',
    ]
    for selector in selectors_valor:
        try:
            await page.fill(selector, valor_str, timeout=3000)
            console.print("[green]✓ Valor preenchido.[/green]")
            break
        except PlaywrightTimeout:
            continue


async def confirmar_e_emitir(page, dry_run: bool) -> None:
    """Confirma a emissão da NFS-e."""
    if dry_run:
        console.print("[yellow]--dry-run ativo: formulário preenchido mas NÃO será submetido.[/yellow]")
        console.print("[yellow]Pressione Ctrl+C para encerrar ou feche o navegador.[/yellow]")
        await asyncio.sleep(60)
        return

    console.print("[cyan]Submetendo NFS-e...[/cyan]")

    submit_selectors = [
        'button[type="submit"]:has-text("Emitir")',
        'button:has-text("Emitir NFS-e")',
        'button:has-text("Confirmar")',
        'button:has-text("Salvar e Emitir")',
        '[data-testid="submit-nfse"]',
    ]
    for selector in submit_selectors:
        try:
            await page.click(selector, timeout=3000)
            break
        except PlaywrightTimeout:
            continue
    else:
        raise RuntimeError(
            "Não encontrou o botão de submissão. "
            "Verifique o seletor em confirmar_e_emitir()."
        )

    # Aguarda confirmação de sucesso
    sucesso_selectors = [
        'text="NFS-e emitida com sucesso"',
        'text="Nota Fiscal emitida"',
        '[class*="success"]',
        '[class*="sucesso"]',
    ]
    for selector in sucesso_selectors:
        try:
            await page.wait_for_selector(selector, timeout=15000)
            console.print("[bold green]✓ NFS-e emitida com sucesso![/bold green]")
            return
        except PlaywrightTimeout:
            continue

    console.print("[yellow]⚠ Submissão realizada, mas não foi possível confirmar o sucesso automaticamente. Verifique o portal.[/yellow]")


async def run(competencia: date, dry_run: bool, config: dict) -> None:
    cpf = os.getenv("GOVBR_CPF") or config["emitente"].get("cpf", "")
    senha = os.getenv("GOVBR_SENHA", "")

    if not cpf or not senha:
        console.print("[red]Erro: defina GOVBR_CPF e GOVBR_SENHA no arquivo .env[/red]")
        sys.exit(1)

    descricao = build_descricao(config, competencia)
    print_preview(config, competencia, descricao)

    if dry_run:
        console.print("[yellow]Modo dry-run: abrindo o portal para visualização.[/yellow]")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=config["portal"]["headless"],
            slow_mo=config["portal"]["slow_mo"],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        try:
            console.print(f"[cyan]Abrindo {config['portal']['url']}...[/cyan]")
            await page.goto(config["portal"]["url"], timeout=config["portal"]["timeout"])

            await login_govbr(page, cpf, senha)
            await navegar_para_emitir(page)
            await preencher_competencia(page, competencia)
            await preencher_tomador(page, config)
            await preencher_servico(page, config, descricao)
            await confirmar_e_emitir(page, dry_run)

        except Exception as exc:
            console.print(f"[bold red]Erro durante a automação:[/bold red] {exc}")
            console.print("[yellow]O navegador permanecerá aberto por 2 minutos para inspeção.[/yellow]")
            await asyncio.sleep(120)
            raise
        finally:
            if not dry_run:
                await browser.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Gera automaticamente a NFS-e mensal no Emissor Nacional (nfse.gov.br)"
    )
    parser.add_argument("--mes", type=int, help="Mês da competência (1-12). Padrão: mês atual.")
    parser.add_argument("--ano", type=int, help="Ano da competência. Padrão: ano atual.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preenche o formulário mas não submete a nota.",
    )
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
