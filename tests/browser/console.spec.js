const { test, expect } = require("@playwright/test");
test.beforeEach(async ({ page, request }) => {
  await request.post("/__reset");
  await page.goto("/");
  await page
    .getByLabel("كلمة مرور الإدارة", { exact: false })
    .fill("browser-test-password");
  await page.getByRole("button", { name: "الدخول إلى لوحة التحكم" }).click();
  await expect(
    page.getByRole("heading", { name: "مركز التحكم." }),
  ).toBeVisible();
});
test("overview, responsive layout and every navigation section loads without browser errors", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const name of [
    "الحسابات والمزوّدون",
    "النماذج والمسارات",
    "المحادثات",
    "مفاتيح الوصول",
    "مختبر المحادثة",
    "سجل النشاط",
    "الربط والتكامل",
    "نظرة عامة",
  ]) {
    await page.getByRole("link", { name, exact: true }).click();
    await expect(page.locator("main h1")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
  await page.screenshot({
    path: `test-results/aurum-${test.info().project.name}.png`,
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("project selection fetches its conversations and saved account settings survive reload", async ({
  page,
}) => {
  await page
    .getByRole("link", { name: "الحسابات والمزوّدون", exact: true })
    .click();
  const card = page.locator(".account-card").filter({
    has: page.getByRole("heading", {
      name: "حساب ChatGPT الأساسي",
      exact: true,
    }),
  });
  await card.getByRole("button", { name: "إدارة الحساب" }).click();
  await page
    .getByRole("combobox", { name: "مشروع ChatGPT", exact: true })
    .selectOption({ label: "Novel project" });
  await expect(
    page.getByRole("option", { name: "Project translation" }),
  ).toBeAttached();
  await page
    .getByRole("combobox", { name: "سلوك المحادثة" })
    .selectOption("fixed");
  await page
    .locator("select[name=conversation_id]")
    .selectOption({ label: "Project translation" });
  await page.getByRole("button", { name: "حفظ إعدادات الحساب" }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await page.reload();
  await card.getByRole("button", { name: "إدارة الحساب" }).click();
  await expect(page.locator("select[name=project_id]")).toHaveValue(
    "novel-project",
  );
  await expect(page.locator("select[name=conversation_id]")).toHaveValue(
    "project-chat",
  );
});
test("Gemini paragraph switch can be disabled and persisted while GPT has no switch", async ({
  page,
}) => {
  await page
    .getByRole("link", { name: "الحسابات والمزوّدون", exact: true })
    .click();
  await page
    .locator(".account-card")
    .filter({ has: page.getByRole("heading", { name: "حساب Gemini الشخصي" }) })
    .getByRole("button", { name: "إدارة الحساب" })
    .click();
  const toggle = page.locator("input[name=gemini_paragraph_mode]");
  await toggle.check();
  await page.getByRole("button", { name: "حفظ إعدادات الحساب" }).click();
  await page
    .locator(".account-card")
    .filter({ has: page.getByRole("heading", { name: "حساب Gemini الشخصي" }) })
    .getByRole("button", { name: "إدارة الحساب" })
    .click();
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await page.getByRole("button", { name: "حفظ إعدادات الحساب" }).click();
  await page.reload();
  await page
    .locator(".account-card")
    .filter({ has: page.getByRole("heading", { name: "حساب Gemini الشخصي" }) })
    .getByRole("button", { name: "إدارة الحساب" })
    .click();
  await expect(toggle).not.toBeChecked();
  await page.getByRole("button", { name: "إغلاق النافذة" }).click();
  await page
    .locator(".account-card")
    .filter({
      has: page.getByRole("heading", { name: "حساب ChatGPT الأساسي" }),
    })
    .getByRole("button", { name: "إدارة الحساب" })
    .click();
  await expect(toggle).toHaveCount(0);
});
test("create client key once and use it for an actual OpenAI-format request", async ({
  page,
  request,
}) => {
  await page.getByRole("link", { name: "مفاتيح الوصول", exact: true }).click();
  await page.getByRole("button", { name: "إنشاء مفتاح وصول" }).click();
  await page
    .getByRole("button", { name: "إنشاء المفتاح", exact: true })
    .click();
  const token = await page.locator("#new-key").textContent();
  expect(token).toMatch(/^sk-proxy-/);
  const response = await request.post("/v1/chat/completions", {
    headers: { Authorization: "Bearer " + token },
    data: {
      model: "gpt",
      messages: [{ role: "user", content: "Hello from OpenMinis" }],
    },
  });
  expect(response.status()).toBe(200);
  expect((await response.json()).choices[0].message.content).toContain(
    "وصل الرد",
  );
  expect(
    await page.evaluate(() =>
      Object.values(localStorage).some((v) => v.includes("sk-proxy-")),
    ),
  ).toBe(false);
  await page.getByRole("button", { name: "إغلاق", exact: true }).click();
  await expect(page.locator("#new-key")).toHaveCount(0);
});
test("create project model route, then test it in the real playground", async ({
  page,
}) => {
  await page
    .getByRole("link", { name: "النماذج والمسارات", exact: true })
    .click();
  await page.getByRole("button", { name: "إضافة مسار نموذج" }).click();
  await page
    .getByRole("textbox", { name: "اسم النموذج في التطبيق" })
    .fill("novel-gpt");
  await page
    .locator("#route-account")
    .selectOption({ label: "حساب ChatGPT الأساسي" });
  await page
    .getByRole("combobox", { name: "مشروع ChatGPT", exact: true })
    .selectOption({ label: "Novel project" });
  await page.getByRole("button", { name: "حفظ مسار النموذج" }).click();
  await expect(page.locator("table")).toContainText("novel-gpt");
  await page.getByRole("link", { name: "مختبر المحادثة", exact: true }).click();
  await page.locator("#play-model").selectOption("novel-gpt");
  await page
    .getByRole("textbox", { name: "رسالة الاختبار" })
    .fill("Test my project conversation");
  await page.getByRole("button", { name: "إرسال", exact: true }).click();
  await expect(page.locator(".chat-bubble").last()).toContainText("وصل الرد");
  await page.getByRole("link", { name: "المحادثات", exact: true }).click();
  await expect(page.locator("table")).toContainText("حساب ChatGPT الأساسي");
});

test("credential replacement keeps the account and reconnects without exposing the secret", async ({
  page,
  request,
}) => {
  await page
    .getByRole("link", { name: "الحسابات والمزوّدون", exact: true })
    .click();
  await page
    .locator(".account-card")
    .filter({
      has: page.getByRole("heading", {
        name: "حساب ChatGPT الأساسي",
        exact: true,
      }),
    })
    .getByRole("button", { name: "إدارة الحساب" })
    .click();
  await page.getByText("تحديث التوكن أو الكوكيز", { exact: true }).click();
  await page
    .locator("textarea[name=credential]")
    .fill("replacement-browser-test-credential");
  await page
    .getByRole("button", { name: "تحديث وربط الحساب", exact: true })
    .click();
  await expect(page.locator("textarea[name=credential]")).toHaveValue("");
  await expect(page.locator("select[name=project_id]")).toHaveValue("");
  const state = await page.request.get("/admin/api/state");
  expect(await state.text()).not.toContain(
    "replacement-browser-test-credential",
  );
  await page.getByRole("button", { name: "حفظ إعدادات الحساب" }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "حساب ChatGPT الأساسي", exact: true }),
  ).toBeVisible();
});
