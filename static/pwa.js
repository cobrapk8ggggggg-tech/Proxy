"use strict";
(() => {
  let installPrompt = null,
    waitingWorker = null;
  const standalone = () =>
    matchMedia("(display-mode: standalone)").matches ||
    navigator.standalone === true;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
  });
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    document.documentElement.classList.add("standalone");
  });
  if (standalone()) document.documentElement.classList.add("standalone");
  window.aurumInstall = async () => {
    if (standalone()) {
      toast("Aurum مثبت بالفعل على شاشتك الرئيسية.");
      return;
    }
    if (installPrompt) {
      await installPrompt.prompt();
      await installPrompt.userChoice;
      installPrompt = null;
      return;
    }
    const ios =
      /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    document.querySelector("#install-description").textContent = ios
      ? "على iPhone أو iPad، افتح هذا الرابط في Safari ثم اتبع الخطوتين:"
      : "ثبّت مساحتك كتطبيق مستقل من قائمة المتصفح:";
    const steps = ios
      ? [
          "اضغط زر المشاركة في Safari.",
          "اختر «إضافة إلى الشاشة الرئيسية»، ثم «إضافة».",
        ]
      : [
          "افتح قائمة المتصفح واختر «تثبيت التطبيق» أو «إضافة إلى الشاشة الرئيسية».",
          "أكّد الإضافة، ثم افتح Aurum من أيقونته.",
        ];
    document.querySelector("#install-steps").replaceChildren(
      ...steps.map((text) => {
        const li = document.createElement("li");
        li.textContent = text;
        return li;
      }),
    );
    document.querySelector("#install-dialog").showModal();
  };
  document
    .querySelector("#close-install")
    .addEventListener("click", () =>
      document.querySelector("#install-dialog").close(),
    );
  if (!("serviceWorker" in navigator)) return;
  const hadController = !!navigator.serviceWorker.controller;
  let reloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController && !reloading) {
      reloading = true;
      location.reload();
    }
  });
  const updateBanner = document.querySelector("#update-banner");
  const loginScreen = document.querySelector("#login");
  const syncUpdateBanner = () => {
    // On small screens the floating banner can cover the login submit button.
    updateBanner.hidden = !waitingWorker || !loginScreen.hidden;
  };
  new MutationObserver(syncUpdateBanner).observe(loginScreen, {
    attributes: true,
    attributeFilter: ["hidden"],
  });
  const offerUpdate = (worker) => {
    waitingWorker = worker;
    syncUpdateBanner();
  };
  document
    .querySelector("#apply-update")
    .addEventListener("click", () =>
      waitingWorker?.postMessage({ type: "ACTIVATE_UPDATE" }),
    );
  navigator.serviceWorker
    .register("/sw.js", { scope: "/" })
    .then((registration) => {
      if (registration.waiting) offerUpdate(registration.waiting);
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        worker?.addEventListener("statechange", () => {
          if (
            worker.state === "installed" &&
            navigator.serviceWorker.controller
          )
            offerUpdate(worker);
        });
      });
    })
    .catch(() => {
      /* The dashboard remains fully usable when installation is unavailable. */
    });
})();
