import { expect, test } from '@playwright/test'

test('标签下钻：运行分析并准备带来源的分组明细导出', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/tag_drill')
  await expect(page.getByTestId('kpi-card').first()).toBeVisible({ timeout: 10000 })

  // 运行下钻
  await page.getByTestId('run-analysis').click()
  await expect(page.locator('.analysis-bar').first()).toBeVisible({ timeout: 5000 })
  await expect(page.locator('.data-table tbody tr').first()).toBeVisible({ timeout: 5000 })
  await expect(page.getByTestId('drill-message')).toContainText('DR-TD-001')

  // 准备分组明细导出文件
  await page.getByTestId('prepare-export').click()
  await expect(page.getByTestId('last-export')).toBeVisible({ timeout: 5000 })
  await expect(page.getByTestId('export-conflict')).toContainText('一致')
  const exportId = (await page.locator('[data-testid="last-export"] div').first().innerText()).replace('文件编号：', '')
  expect(exportId).toMatch(/^DE-TD-001-\d{4}$/)

  // 导出文件列表出现该文件
  await expect(page.locator(`[data-testid="export-row-${exportId}"]`)).toBeVisible()

  // 子标签转到新父级后，重新准备导出应出现层级冲突提示
  await page.getByTestId('move-tag').click()
  await expect(page.getByTestId('drill-message')).toContainText('TAG-002')
  await page.getByTestId('prepare-export').click()
  await expect(page.getByTestId('export-conflict')).toContainText('冲突')

  expect(errors).toEqual([])
})

test('标签快照：归档失败重试只交付一次，并可下载与来源回查', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400 && !r.url().includes('/archives')) errors.push(`${r.status()} ${r.url()}`) })

  // 先在下钻页准备一个导出文件
  await page.goto('/tag_drill')
  await page.getByTestId('run-analysis').click()
  await expect(page.locator('.analysis-bar').first()).toBeVisible({ timeout: 5000 })
  await page.getByTestId('prepare-export').click()
  await expect(page.getByTestId('last-export')).toBeVisible({ timeout: 5000 })
  const exportRow = page.locator('[data-testid^="export-row-DE-"]').first()
  const exportId = (await exportRow.getAttribute('data-testid') || '').replace('export-row-', '')

  // 快照页提交归档，先制造写入后失败
  await page.goto('/tag_snapshot')
  await expect(page.getByTestId('archive-panel')).toBeVisible()
  await page.getByTestId('archive-id').fill('ARC-E2E-2')
  await page.getByTestId('archive-export-id').fill(exportId)
  await page.getByTestId('archive-failure').selectOption('after_write')
  await page.getByTestId('submit-archive').click()
  await expect(page.getByTestId('archive-message')).toContainText('归档未确认')

  // 未确认时下载按钮禁用
  const row = page.locator(`[data-testid="archive-row-ARC-E2E-2"]`)
  await expect(row).toBeVisible()
  await expect(row.getByRole('button', { name: '下载' })).toBeDisabled()

  // 原请求补交（去掉故障注入）
  await page.getByTestId('archive-failure').selectOption('')
  await page.getByTestId('submit-archive').click()
  await expect(page.getByTestId('archive-message')).toContainText('归档已确认')

  // 再提交一次：幂等返回原文件
  await page.getByTestId('submit-archive').click()
  await expect(page.getByTestId('archive-message')).toContainText('幂等返回原文件')

  // 下载按钮可用，来源回查全部一致
  await expect(row.getByRole('button', { name: '下载' })).toBeEnabled()
  await row.getByRole('button', { name: '来源回查' }).click()
  await expect(page.getByTestId('trace-result')).toBeVisible()
  await expect(page.getByTestId('trace-result')).toContainText('一致')

  expect(errors).toEqual([])
})
