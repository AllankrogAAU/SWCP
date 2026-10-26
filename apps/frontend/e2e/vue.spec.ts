import { test, expect } from '@playwright/test'

test('visits the app root url', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('SWCP code feedback')).toBeVisible()
  await expect(page.getByRole('button', { name: /Sign in/i })).toBeVisible()
})
