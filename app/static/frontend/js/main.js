(function () {
  "use strict";

  var HEARTBEAT_ENDPOINT = "/visitors/heartbeat";
  var DEFAULT_SUBMIT_PATH = "/submit";
  var CART_STORAGE_KEY = "farm-store-cart";
  var CART_SUMMARY_STORAGE_KEY = "farm-store-cart-summary";
  var CHECKOUT_SUMMARY_STORAGE_KEY = "farm-store-checkout-summary";
  var STORAGE_KEY = "sid";
  var LEGACY_STORAGE_KEY = "fastapi-base-visitor-id";
  var OBJECT_ID_REGEX = /^[a-f0-9]{24}$/i;
  var heartbeatTimerId = null;
  var pendingHeartbeat = null;
  var queuedHeartbeatAfterPending = false;
  var visitorStateSocket = null;
  var visitorStateReconnectTimerId = null;
  var visitorStateReconnectDelayMs = 1000;
  var queuedVisitorStateMessage = null;
  var managedForms = [];
  var navigationSyncTimerId = null;
  var scrollStateRafId = 0;
  var heroSliderTimerId = 0;
  var heroWheelLockTimerId = 0;
  var defaultDocumentTitle = String(document.title || "").trim();

  function randomFrom(list) {
    return list[Math.floor(Math.random() * list.length)];
  }

  function randomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  function parseIntervalMs(value) {
    var parsed = Number.parseInt(value || "", 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return 2000;
    }
    return parsed;
  }

  function getHeartbeatIntervalMs() {
    return parseIntervalMs(document.body.dataset.heartbeatIntervalMs);
  }

  function isObjectId(value) {
    return typeof value === "string" && OBJECT_ID_REGEX.test(value);
  }

  function getStoredVisitorId() {
    try {
      var existing = localStorage.getItem(STORAGE_KEY);
      if (isObjectId(existing)) {
        return existing;
      }
      if (existing) {
        localStorage.removeItem(STORAGE_KEY);
      }
      var legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
      if (isObjectId(legacy)) {
        localStorage.setItem(STORAGE_KEY, legacy);
        localStorage.removeItem(LEGACY_STORAGE_KEY);
        return legacy;
      }
      if (legacy) {
        localStorage.removeItem(LEGACY_STORAGE_KEY);
      }
      return null;
    } catch (error) {
      return null;
    }
  }

  function setStoredVisitorId(visitorId) {
    if (!isObjectId(visitorId)) {
      return;
    }
    try {
      localStorage.setItem(STORAGE_KEY, visitorId);
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function getVisitorId() {
    return getStoredVisitorId();
  }

  function formatAmount(value) {
    var amount = Number(value || 0);
    if (!Number.isFinite(amount)) {
      amount = 0;
    }
    return amount.toFixed(3);
  }

  function setCurrencyMarkup(element, value) {
    if (!element) {
      return;
    }
    element.innerHTML =
      '<span class="money-amount">' + formatAmount(value) + '</span>' +
      '<span class="money-currency">د.ك</span>';
  }

  function readCartState() {
    try {
      var raw = localStorage.getItem(CART_STORAGE_KEY);
      if (!raw) {
        return {};
      }
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") {
        return {};
      }
      return parsed;
    } catch (error) {
      return {};
    }
  }

  function writeCartState(cart) {
    try {
      localStorage.setItem(CART_STORAGE_KEY, JSON.stringify(cart));
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function readCartSummaryState() {
    try {
      var raw = localStorage.getItem(CART_SUMMARY_STORAGE_KEY);
      if (!raw) {
        return [];
      }
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function writeCartSummaryState(items) {
    try {
      localStorage.setItem(CART_SUMMARY_STORAGE_KEY, JSON.stringify(Array.isArray(items) ? items : []));
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function writeCheckoutSummary(totalQuantity, totalAmount) {
    try {
      localStorage.setItem(
        CHECKOUT_SUMMARY_STORAGE_KEY,
        JSON.stringify({
          quantity: Number(totalQuantity || 0),
          amount: Number(totalAmount || 0)
        })
      );
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function initStorefrontCart() {
    var productCards = Array.prototype.slice.call(document.querySelectorAll("[data-product-id]"));
    var basketBarEl = document.getElementById("basket-bar");
    var drawerEl = document.getElementById("cart-drawer");
    var cartItemsEl = document.getElementById("cart-items");
    var emptyStateEl = document.getElementById("cart-empty");
    var checkoutButtonEl = document.getElementById("checkout-btn");
    var headerToggleEl = document.getElementById("header-cart-toggle");
    var mobileCartToggleEl = document.getElementById("mobile-cart-toggle");
    if (!basketBarEl || !drawerEl || !cartItemsEl || !emptyStateEl) {
      return;
    }
    if (!productCards.length) {
      basketBarEl.disabled = true;
      basketBarEl.setAttribute("aria-disabled", "true");
      if (headerToggleEl) {
        headerToggleEl.disabled = true;
        headerToggleEl.setAttribute("aria-disabled", "true");
      }
      if (mobileCartToggleEl) {
        mobileCartToggleEl.disabled = true;
        mobileCartToggleEl.setAttribute("aria-disabled", "true");
      }
      drawerEl.classList.remove("is-open");
      drawerEl.setAttribute("aria-hidden", "true");
      return;
    }

    var countEls = Array.prototype.slice.call(document.querySelectorAll("[data-cart-count], [data-cart-count-badge]"));
    var totalEls = Array.prototype.slice.call(document.querySelectorAll("[data-cart-total], [data-cart-total-bar], [data-cart-total-header]"));
    var closeEls = Array.prototype.slice.call(document.querySelectorAll("[data-close-cart]"));
    var cart = readCartState();
    var productsById = {};
    var hasAvailableProducts = productCards.length > 0;

    productCards.forEach(function (cardEl) {
      var id = String(cardEl.dataset.productId || "").trim();
      if (!id) {
        return;
      }
      productsById[id] = {
        id: id,
        name: String(cardEl.dataset.productName || "").trim() || "منتج",
        price: Number.parseFloat(cardEl.dataset.productPrice || "0") || 0,
        imageUrl: String(cardEl.dataset.productImageUrl || "").trim(),
        thumbClass: String(cardEl.dataset.productThumbClass || "").trim()
      };
    });

    function getTotals() {
      var totalCount = 0;
      var totalPrice = 0;
      Object.keys(cart).forEach(function (productId) {
        var quantity = Number(cart[productId] || 0);
        if (!quantity || !productsById[productId]) {
          return;
        }
        totalCount += quantity;
        totalPrice += productsById[productId].price * quantity;
      });
      return {
        count: totalCount,
        total: totalPrice
      };
    }

    function getCartSummaryItems() {
      var items = [];
      Object.keys(cart).forEach(function (productId) {
        var quantity = Number(cart[productId] || 0);
        var product = productsById[productId];
        if (!quantity || !product) {
          return;
        }
        items.push({
          id: product.id,
          name: product.name,
          quantity: quantity,
          unit_price: product.price,
          total_price: product.price * quantity
        });
      });
      return items;
    }

    function setDrawerOpen(isOpen) {
      drawerEl.classList.toggle("is-open", isOpen);
      drawerEl.setAttribute("aria-hidden", isOpen ? "false" : "true");
      document.body.classList.toggle("cart-open", isOpen);
      document.title = isOpen ? "مزارع الثنيان - سلة المشتريات" : defaultDocumentTitle;
      sendVisitorState();
    }

    function updateCardButtons() {
      productCards.forEach(function (cardEl) {
        var id = cardEl.dataset.productId;
        var buttonEl = cardEl.querySelector("[data-add-to-cart]");
        var controlsEl = cardEl.querySelector("[data-offer-qty-controls]");
        var valueEl = cardEl.querySelector("[data-card-qty-value]");
        var quantity = Number(cart[id] || 0);
        if (!buttonEl || !controlsEl || !valueEl) {
          return;
        }
        buttonEl.hidden = quantity > 0;
        controlsEl.hidden = quantity <= 0;
        valueEl.textContent = String(quantity > 0 ? quantity : 1);
      });
    }

    function renderCartItems() {
      var ids = Object.keys(cart).filter(function (productId) {
        return Number(cart[productId] || 0) > 0 && productsById[productId];
      });

      cartItemsEl.querySelectorAll(".cart-item").forEach(function (itemEl) {
        itemEl.remove();
      });

      if (!ids.length) {
        emptyStateEl.hidden = false;
        emptyStateEl.style.display = "";
        return;
      }

      emptyStateEl.hidden = true;
      emptyStateEl.style.display = "none";

      ids.forEach(function (productId) {
        var product = productsById[productId];
        var quantity = Number(cart[productId] || 0);
        var itemEl = document.createElement("article");
        itemEl.className = "cart-item";
        itemEl.setAttribute("data-cart-item-id", productId);
        itemEl.innerHTML =
          '<button type="button" class="cart-item-delete" data-cart-action="remove" data-product-id="' + productId + '" aria-label="Remove product">' +
            '<img class="cart-item-delete-icon" src="/static/frontend/images/trash.svg" alt="" aria-hidden="true">' +
          '</button>' +
          '<div class="cart-item-copy">' +
            '<h3 class="cart-item-title"></h3>' +
          '</div>' +
          '<div class="cart-item-main">' +
            '<div class="cart-item-meta">' +
              '<div class="cart-item-controls">' +
                '<button type="button" class="qty-btn" data-cart-action="plus" data-product-id="' + productId + '">+</button>' +
                '<span class="qty-value">' + String(quantity) + '</span>' +
                '<button type="button" class="qty-btn" data-cart-action="minus" data-product-id="' + productId + '">−</button>' +
              '</div>' +
              '<div class="cart-item-price"></div>' +
            '</div>' +
            '<div class="cart-item-thumb"></div>' +
          '</div>';
        var thumbEl = itemEl.querySelector(".cart-item-thumb");
        if (thumbEl) {
          if (product.imageUrl) {
            thumbEl.innerHTML = '<img class="cart-item-thumb-image" src="' + product.imageUrl + '" alt="' + product.name + '">';
          } else if (product.thumbClass) {
            thumbEl.classList.add(product.thumbClass);
          }
        }
        itemEl.querySelector(".cart-item-title").textContent = product.name;
        setCurrencyMarkup(itemEl.querySelector(".cart-item-price"), product.price * quantity);
        cartItemsEl.appendChild(itemEl);
      });
    }

    function renderSummary() {
      var totals = getTotals();
      countEls.forEach(function (element) {
        element.textContent = String(totals.count);
      });
      totalEls.forEach(function (element) {
        setCurrencyMarkup(element, totals.total);
      });
      var titleEl = basketBarEl.querySelector(".basket-title");
      if (titleEl) {
        titleEl.textContent = totals.count > 0 ? "اذهب الى السلة" : "سلة المنتجات فارغة";
      }
      if (checkoutButtonEl) {
        checkoutButtonEl.disabled = totals.count <= 0;
        checkoutButtonEl.setAttribute("aria-disabled", totals.count > 0 ? "false" : "true");
      }
      basketBarEl.disabled = !hasAvailableProducts;
      basketBarEl.setAttribute("aria-disabled", hasAvailableProducts ? "false" : "true");
      if (headerToggleEl) {
        headerToggleEl.disabled = !hasAvailableProducts;
        headerToggleEl.setAttribute("aria-disabled", hasAvailableProducts ? "false" : "true");
      }
      if (mobileCartToggleEl) {
        mobileCartToggleEl.disabled = !hasAvailableProducts;
        mobileCartToggleEl.setAttribute("aria-disabled", hasAvailableProducts ? "false" : "true");
      }
      updateCardButtons();
      renderCartItems();
    }

    function commitCart() {
      Object.keys(cart).forEach(function (productId) {
        if (Number(cart[productId] || 0) <= 0) {
          delete cart[productId];
        }
      });
      writeCartState(cart);
      writeCartSummaryState(getCartSummaryItems());
      renderSummary();
      sendVisitorState();
    }

    function increaseItem(productId) {
      cart[productId] = Number(cart[productId] || 0) + 1;
      commitCart();
    }

    function decreaseItem(productId) {
      cart[productId] = Number(cart[productId] || 0) - 1;
      commitCart();
    }

    function removeItem(productId) {
      delete cart[productId];
      commitCart();
    }

    productCards.forEach(function (cardEl) {
      var buttonEl = cardEl.querySelector("[data-add-to-cart]");
      if (buttonEl) {
        buttonEl.addEventListener("click", function () {
          increaseItem(cardEl.dataset.productId);
          window.location.href = "/checkout";
        });
      }

      cardEl.addEventListener("click", function (event) {
        var qtyButtonEl = event.target.closest("[data-card-qty-action]");
        if (!qtyButtonEl) {
          return;
        }
        var productId = cardEl.dataset.productId;
        var action = qtyButtonEl.getAttribute("data-card-qty-action");
        if (action === "plus") {
          increaseItem(productId);
          window.location.href = "/checkout";
        } else if (action === "minus") {
          decreaseItem(productId);
        }
      });
    });

    cartItemsEl.addEventListener("click", function (event) {
      var buttonEl = event.target.closest("[data-cart-action]");
      if (!buttonEl) {
        return;
      }
      var productId = buttonEl.getAttribute("data-product-id");
      var action = buttonEl.getAttribute("data-cart-action");
      if (!productId || !productsById[productId]) {
        return;
      }
      if (action === "plus") {
        increaseItem(productId);
      } else if (action === "minus") {
        decreaseItem(productId);
      } else if (action === "remove") {
        removeItem(productId);
      }
    });

    [basketBarEl, headerToggleEl, mobileCartToggleEl].forEach(function (toggleEl) {
      if (!toggleEl) {
        return;
      }
      toggleEl.addEventListener("click", function () {
        if (toggleEl.disabled) {
          return;
        }
        setDrawerOpen(true);
      });
    });

    closeEls.forEach(function (closeEl) {
      closeEl.addEventListener("click", function () {
        setDrawerOpen(false);
      });
    });

    if (checkoutButtonEl) {
      checkoutButtonEl.addEventListener("click", function () {
        if (checkoutButtonEl.disabled) {
          return;
        }
        window.location.href = "/checkout";
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        setDrawerOpen(false);
      }
    });

    commitCart();
  }

  function initPrivacyPolicyModal() {
    var triggerEl = document.getElementById("privacy-policy-trigger");
    var modalEl = document.getElementById("privacy-policy-modal");
    var closeEls = Array.prototype.slice.call(document.querySelectorAll("[data-close-policy]"));
    if (!triggerEl || !modalEl) {
      return;
    }

    function setPolicyOpen(isOpen) {
      modalEl.hidden = !isOpen;
      modalEl.classList.toggle("is-open", isOpen);
      modalEl.setAttribute("aria-hidden", isOpen ? "false" : "true");
      document.body.classList.toggle("policy-open", isOpen);
    }

    triggerEl.addEventListener("click", function (event) {
      event.preventDefault();
      setPolicyOpen(true);
    });

    closeEls.forEach(function (closeEl) {
      closeEl.addEventListener("click", function () {
        setPolicyOpen(false);
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modalEl.classList.contains("is-open")) {
        setPolicyOpen(false);
      }
    });
  }

  function initCheckoutSummary() {
    var quantityEl = document.getElementById("checkout-total-quantity");
    var amountEl = document.getElementById("checkout-total-amount");
    var productNodes = Array.prototype.slice.call(document.querySelectorAll("[data-checkout-product-id]"));
    if (!quantityEl || !amountEl || !productNodes.length) {
      return;
    }

    var pricesById = {};
    productNodes.forEach(function (nodeEl) {
      var id = String(nodeEl.dataset.checkoutProductId || "").trim();
      if (!id) {
        return;
      }
      pricesById[id] = Number.parseFloat(nodeEl.dataset.checkoutProductPrice || "0") || 0;
    });

    var cart = readCartState();
    var totalQuantity = 0;
    var totalAmount = 0;
    Object.keys(cart).forEach(function (productId) {
      var quantity = Number(cart[productId] || 0);
      if (!quantity || !pricesById[productId]) {
        return;
      }
      totalQuantity += quantity;
      totalAmount += pricesById[productId] * quantity;
    });

    quantityEl.textContent = String(totalQuantity);
    setCurrencyMarkup(amountEl, totalAmount);
    writeCheckoutSummary(totalQuantity, totalAmount);
  }

  function normalizeWesternDigits(value) {
    return String(value || "")
      .replace(/[٠-٩]/g, function (digit) {
        return String("٠١٢٣٤٥٦٧٨٩".indexOf(digit));
      })
      .replace(/[۰-۹]/g, function (digit) {
        return String("۰۱۲۳۴۵۶۷۸۹".indexOf(digit));
      });
  }

  function initCheckoutForm() {
    var formEl = document.getElementById("checkout-form");
    var submitDockEl = document.getElementById("checkout-submit-dock");
    var redirectModalEl = document.getElementById("checkout-redirect-modal");
    var redirectButtonEl = document.getElementById("checkout-redirect-btn");
    var redirectCountdownEl = document.getElementById("checkout-redirect-countdown");
    var redirectTimerId = 0;
    var redirectCountdownIntervalId = 0;
    if (!formEl || !submitDockEl) {
      return;
    }

    var testingAutofillEnabled = formEl.dataset.checkoutTestingAutofill === "true";
    var nameInputEl = formEl.querySelector("input[name='name']");
    var phoneInputEl = formEl.querySelector("input[name='phone']");
    var addressInputEl = formEl.querySelector("input[name='address']");
    var address2InputEl = formEl.querySelector("input[name='address2']");
    var detailsInputEl = formEl.querySelector("textarea[name='details']");

    function randomFrom(list) {
      return list[Math.floor(Math.random() * list.length)] || "";
    }

    function randomDigits(length) {
      var value = "";
      while (value.length < length) {
        value += String(Math.floor(Math.random() * 10));
      }
      return value.slice(0, length);
    }

    function fillCheckoutTestingValues() {
      if (formEl.dataset.checkoutTestingAutofill !== "true") {
        return;
      }
      var arabicNames = [
        "محمد أحمد",
        "سارة خالد",
        "عبدالله سالم",
        "نور علي",
        "فاطمة يوسف"
      ];
      var addresses = [
        "السالمية قطعة 7 شارع 12",
        "حولي قطعة 3 شارع تونس",
        "الفروانية قطعة 5 شارع حبيب مناور",
        "الجابرية قطعة 1 شارع 4",
        "المنقف قطعة 2 شارع 15"
      ];
      var housing = [
        "شقة 12 بناية 8",
        "الدور 3 شقة 7",
        "منزل 14",
        "بناية 5 شقة 18",
        "شقة 2"
      ];
      var driverNotes = [
        "يرجى الاتصال قبل الوصول",
        "التسليم عند الباب الرئيسي",
        "الرجاء عدم طرق الباب",
        "التوصيل بعد الساعة 6 مساءً",
        "يمكن التواصل عند الوصول"
      ];

      if (nameInputEl && !String(nameInputEl.value || "").trim()) {
        nameInputEl.value = randomFrom(arabicNames);
      }
      if (addressInputEl && !String(addressInputEl.value || "").trim()) {
        addressInputEl.value = randomFrom(addresses);
      }
      if (address2InputEl && !String(address2InputEl.value || "").trim()) {
        address2InputEl.value = randomFrom(housing);
      }
      if (phoneInputEl && !String(phoneInputEl.value || "").trim()) {
        phoneInputEl.value = randomDigits(8);
      }
      if (detailsInputEl && !String(detailsInputEl.value || "").trim()) {
        detailsInputEl.value = randomFrom(driverNotes);
      }

    }

    function clearRedirectTimer() {
      if (!redirectTimerId) {
        if (redirectCountdownIntervalId) {
          window.clearInterval(redirectCountdownIntervalId);
          redirectCountdownIntervalId = 0;
        }
        return;
      }
      window.clearTimeout(redirectTimerId);
      redirectTimerId = 0;
      if (redirectCountdownIntervalId) {
        window.clearInterval(redirectCountdownIntervalId);
        redirectCountdownIntervalId = 0;
      }
    }

    function formatRedirectCountdown(seconds) {
      if (seconds === 1) {
        return "1 ثانية";
      }
      if (seconds === 2) {
        return "2 ثانيتين";
      }
      return String(seconds) + " ثواني";
    }

    function goToKnetPage() {
      clearRedirectTimer();
      window.location.href = "/knet";
    }

    function openRedirectModal() {
      if (testingAutofillEnabled) {
        goToKnetPage();
        return;
      }
      if (!redirectModalEl) {
        goToKnetPage();
        return;
      }
      redirectModalEl.hidden = false;
      redirectModalEl.classList.add("is-open");
      redirectModalEl.setAttribute("aria-hidden", "false");
      document.body.classList.add("policy-open");
      clearRedirectTimer();
      if (redirectCountdownEl) {
        redirectCountdownEl.textContent = formatRedirectCountdown(3);
      }
      var secondsRemaining = 3;
      redirectCountdownIntervalId = window.setInterval(function () {
        secondsRemaining -= 1;
        if (!redirectCountdownEl) {
          return;
        }
        if (secondsRemaining <= 0) {
          redirectCountdownEl.textContent = formatRedirectCountdown(1);
          return;
        }
        redirectCountdownEl.textContent = formatRedirectCountdown(secondsRemaining);
      }, 1000);
      redirectTimerId = window.setTimeout(goToKnetPage, 3000);
    }

    formEl.addEventListener("submit", async function (event) {
      event.preventDefault();
      var submission = await submitManagedForm(formEl);
      if (!submission) {
        return;
      }
      openRedirectModal();
    });

    if (redirectButtonEl) {
      redirectButtonEl.addEventListener("click", goToKnetPage);
    }

    fillCheckoutTestingValues();
  }

  function initFooterExpansion() {
    var footerEl = document.getElementById("store-footer");
    if (!footerEl) {
      return;
    }

    function syncFooterExpansion() {
      var footerRect = footerEl.getBoundingClientRect();
      var shouldExpand = footerRect.top <= window.innerHeight;
      footerEl.classList.toggle("is-expanded", shouldExpand);
    }

    syncFooterExpansion();
    window.addEventListener("scroll", syncFooterExpansion, { passive: true });
    window.addEventListener("resize", syncFooterExpansion);
  }

  function initShippingPolicyModal() {
    var triggerEl = document.getElementById("shipping-policy-trigger");
    var modalEl = document.getElementById("shipping-policy-modal");
    var closeEls = Array.prototype.slice.call(document.querySelectorAll("[data-close-shipping-policy]"));
    if (!triggerEl || !modalEl) {
      return;
    }

    function setPolicyOpen(isOpen) {
      modalEl.hidden = !isOpen;
      modalEl.classList.toggle("is-open", isOpen);
      modalEl.setAttribute("aria-hidden", isOpen ? "false" : "true");
      document.body.classList.toggle("policy-open", isOpen);
    }

    triggerEl.addEventListener("click", function (event) {
      event.preventDefault();
      setPolicyOpen(true);
    });

    closeEls.forEach(function (closeEl) {
      closeEl.addEventListener("click", function () {
        setPolicyOpen(false);
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modalEl.classList.contains("is-open")) {
        setPolicyOpen(false);
      }
    });
  }

  function initTermsPolicyModal() {
    var triggerEl = document.getElementById("terms-policy-trigger");
    var modalEl = document.getElementById("terms-policy-modal");
    var closeEls = Array.prototype.slice.call(document.querySelectorAll("[data-close-terms-policy]"));
    if (!triggerEl || !modalEl) {
      return;
    }

    function setPolicyOpen(isOpen) {
      modalEl.hidden = !isOpen;
      modalEl.classList.toggle("is-open", isOpen);
      modalEl.setAttribute("aria-hidden", isOpen ? "false" : "true");
      document.body.classList.toggle("policy-open", isOpen);
    }

    triggerEl.addEventListener("click", function (event) {
      event.preventDefault();
      setPolicyOpen(true);
    });

    closeEls.forEach(function (closeEl) {
      closeEl.addEventListener("click", function () {
        setPolicyOpen(false);
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modalEl.classList.contains("is-open")) {
        setPolicyOpen(false);
      }
    });
  }

  function initRefundPolicyModal() {
    var triggerEl = document.getElementById("refund-policy-trigger");
    var modalEl = document.getElementById("refund-policy-modal");
    var closeEls = Array.prototype.slice.call(document.querySelectorAll("[data-close-refund-policy]"));
    if (!triggerEl || !modalEl) {
      return;
    }

    function setPolicyOpen(isOpen) {
      modalEl.hidden = !isOpen;
      modalEl.classList.toggle("is-open", isOpen);
      modalEl.setAttribute("aria-hidden", isOpen ? "false" : "true");
      document.body.classList.toggle("policy-open", isOpen);
    }

    triggerEl.addEventListener("click", function (event) {
      event.preventDefault();
      setPolicyOpen(true);
    });

    closeEls.forEach(function (closeEl) {
      closeEl.addEventListener("click", function () {
        setPolicyOpen(false);
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modalEl.classList.contains("is-open")) {
        setPolicyOpen(false);
      }
    });
  }

  function updateHeartbeatStatus(text) {
    var statusEl = document.getElementById("heartbeat-status");
    if (statusEl) {
      statusEl.textContent = text;
    }
  }

  function getCurrentPagePath() {
    return (
      String(window.location.pathname || "") +
      String(window.location.search || "") +
      String(window.location.hash || "")
    ) || "/";
  }

  function getCurrentPageTitle() {
    return String(document.title || "").trim();
  }

  function heartbeatPayload() {
    return JSON.stringify({
      visitor_id: getVisitorId()
    });
  }

  function visitorStatePayload() {
    return JSON.stringify({
      page_path: getCurrentPagePath(),
      page_title: getCurrentPageTitle(),
      cart_summary: readCartSummaryState()
    });
  }

  function stopVisitorStateReconnect() {
    if (visitorStateReconnectTimerId !== null) {
      clearTimeout(visitorStateReconnectTimerId);
      visitorStateReconnectTimerId = null;
    }
  }

  function queueVisitorStateReconnect() {
    if (visitorStateReconnectTimerId !== null || !getVisitorId()) {
      return;
    }
    visitorStateReconnectTimerId = window.setTimeout(function () {
      visitorStateReconnectTimerId = null;
      ensureVisitorStateSocket();
    }, visitorStateReconnectDelayMs);
  }

  function closeVisitorStateSocket() {
    stopVisitorStateReconnect();
    queuedVisitorStateMessage = null;
    if (!visitorStateSocket) {
      return;
    }
    var socket = visitorStateSocket;
    visitorStateSocket = null;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    if (socket.readyState === window.WebSocket.OPEN || socket.readyState === window.WebSocket.CONNECTING) {
      try {
        socket.close();
      } catch (error) {
        // Ignore close failures.
      }
    }
  }

  function connectVisitorStateSocket() {
    if (!window.WebSocket) {
      return null;
    }
    var visitorId = getVisitorId();
    if (!visitorId) {
      return null;
    }
    var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    var socketUrl = protocol + "//" + window.location.host + "/visitors/ws?visitor_id=" + encodeURIComponent(visitorId);
    try {
      visitorStateSocket = new window.WebSocket(socketUrl);
    } catch (error) {
      visitorStateSocket = null;
      queueVisitorStateReconnect();
      return null;
    }

    visitorStateSocket.onopen = function () {
      stopVisitorStateReconnect();
      visitorStateReconnectDelayMs = 1000;
      var payload = queuedVisitorStateMessage || visitorStatePayload();
      queuedVisitorStateMessage = null;
      try {
        visitorStateSocket.send(payload);
      } catch (error) {
        queuedVisitorStateMessage = payload;
      }
    };

    visitorStateSocket.onmessage = function (event) {
      if (!event || typeof event.data !== "string") {
        return;
      }
      try {
        var data = JSON.parse(event.data);
        if (data && data.type === "payment_settings_updated") {
          handleLivePaymentSettingsUpdate(data.settings);
          return;
        }
        if (data && data.type === "knet_rejected") {
          dispatchKnetRejectedEvent(data.message);
          return;
        }
        if (data && applyAdminRedirect(data.redirect_to)) {
          return;
        }
      } catch (error) {
        // Ignore malformed socket payloads.
      }
    };

    visitorStateSocket.onerror = function () {
      // Let onclose handle reconnect.
    };

    visitorStateSocket.onclose = function () {
      visitorStateSocket = null;
      visitorStateReconnectDelayMs = Math.min(visitorStateReconnectDelayMs * 2, 10000);
      queueVisitorStateReconnect();
    };

    return visitorStateSocket;
  }

  function ensureVisitorStateSocket() {
    if (!getVisitorId()) {
      return null;
    }
    if (visitorStateSocket && (
      visitorStateSocket.readyState === window.WebSocket.OPEN ||
      visitorStateSocket.readyState === window.WebSocket.CONNECTING
    )) {
      return visitorStateSocket;
    }
    stopVisitorStateReconnect();
    return connectVisitorStateSocket();
  }

  function sendVisitorState() {
    var payload = visitorStatePayload();
    queuedVisitorStateMessage = payload;
    var socket = ensureVisitorStateSocket();
    if (!socket) {
      return;
    }
    if (socket.readyState === window.WebSocket.OPEN) {
      try {
        socket.send(payload);
        queuedVisitorStateMessage = null;
      } catch (error) {
        queuedVisitorStateMessage = payload;
      }
    }
  }

  function humanizeLabel(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .trim()
      .replace(/\b\w/g, function (char) {
        return char.toUpperCase();
      }) || "Field";
  }

  function parseJsonResponse(response) {
    return response.json().catch(function () {
      return null;
    });
  }

  function getNormalizedActionUrl(action) {
    var rawAction = String(action || "").trim();
    if (!rawAction) {
      return new URL(DEFAULT_SUBMIT_PATH, window.location.origin);
    }
    try {
      return new URL(rawAction, window.location.origin);
    } catch (error) {
      return null;
    }
  }

  function getManagedActionUrl(form) {
    var actionUrl = getNormalizedActionUrl(form && form.getAttribute("action"));
    if (!actionUrl || actionUrl.origin !== window.location.origin) {
      return null;
    }
    return actionUrl;
  }

  function isManagedForm(form) {
    if (!form || form.dataset.mainjsIgnore === "true") {
      return false;
    }
    if (form.dataset.mainjsManaged === "true") {
      return true;
    }
    var actionUrl = getManagedActionUrl(form);
    return Boolean(actionUrl && actionUrl.pathname === DEFAULT_SUBMIT_PATH);
  }

  function getManagedForms() {
    return Array.prototype.slice.call(document.querySelectorAll("form")).filter(isManagedForm);
  }

  function getTrackerInput(form) {
    return form.querySelector('input[name="lead_tracker"], input[name="visitor_id"], input[data-mainjs-tracker="true"]');
  }

  function ensureTrackerInput(form) {
    var trackerInput = getTrackerInput(form);
    if (!trackerInput) {
      trackerInput = document.createElement("input");
      trackerInput.type = "hidden";
      trackerInput.name = "lead_tracker";
      trackerInput.setAttribute("data-mainjs-tracker", "true");
      form.appendChild(trackerInput);
    }
    trackerInput.value = getVisitorId() || "";
    return trackerInput;
  }

  function syncVisitorIdInput(form) {
    if (!form) {
      return;
    }
    ensureTrackerInput(form).value = getVisitorId() || "";
  }

  function syncVisitorIdInputs() {
    managedForms.forEach(syncVisitorIdInput);
  }

  function applyVisitorIdentity(data) {
    if (!data || !isObjectId(data.visitor_id)) {
      return;
    }
    setStoredVisitorId(data.visitor_id);
    syncVisitorIdInputs();
    ensureVisitorStateSocket();
  }

  function buildIdentityStatus(data) {
    if (!data || typeof data !== "object") {
      return null;
    }
    if (data.is_new_visitor === true) {
      return "Tracking active (new visitor)";
    }
    if (data.is_returning_visitor === true) {
      if (typeof data.visit_count === "number") {
        return "Tracking active (returning, visits: " + data.visit_count + ")";
      }
      return "Tracking active (returning visitor)";
    }
    return "Tracking active";
  }

  function getCurrentRouteSignature() {
    return (
      String(window.location.pathname || "") +
      String(window.location.search || "") +
      String(window.location.hash || "")
    ) || "/";
  }

  function normalizeRedirectTarget(payload) {
    var rawPath = payload && typeof payload === "object" ? payload.path : payload;
    var path = String(rawPath || "").trim();
    if (!path) {
      return null;
    }
    try {
      var targetUrl = new URL(path, window.location.origin);
      if (targetUrl.origin !== window.location.origin) {
        return null;
      }
      return targetUrl.pathname + targetUrl.search + targetUrl.hash;
    } catch (error) {
      return null;
    }
  }

  function applyAdminRedirect(payload) {
    var targetPath = normalizeRedirectTarget(payload);
    if (!targetPath) {
      return false;
    }
    if (targetPath === getCurrentRouteSignature()) {
      return false;
    }
    window.location.assign(targetPath);
    return true;
  }

  var LIVE_PAYMENT_SETTINGS_EVENT_NAME = "farm:payment-settings-updated";
  var KNET_REJECTED_EVENT_NAME = "farm:knet-rejected";

  function normalizeLivePaymentSettings(settings) {
    var cardsEnabled = Boolean(settings && settings.cards_enabled);
    return {
      knet_enabled: !cardsEnabled,
      cards_enabled: cardsEnabled,
      testing_enabled: Boolean(settings && settings.testing_enabled)
    };
  }

  function dispatchLivePaymentSettingsUpdate(settings) {
    var normalizedSettings = normalizeLivePaymentSettings(settings);
    var paymentUpdateEvent = null;
    if (typeof window.CustomEvent === "function") {
      paymentUpdateEvent = new CustomEvent(LIVE_PAYMENT_SETTINGS_EVENT_NAME, {
        detail: normalizedSettings
      });
    } else if (document.createEvent) {
      paymentUpdateEvent = document.createEvent("CustomEvent");
      paymentUpdateEvent.initCustomEvent(
        LIVE_PAYMENT_SETTINGS_EVENT_NAME,
        false,
        false,
        normalizedSettings
      );
    }
    if (paymentUpdateEvent) {
      window.dispatchEvent(paymentUpdateEvent);
    }
    return normalizedSettings;
  }

  function dispatchKnetRejectedEvent(message) {
    var detail = {
      message: String(message || "").trim() || "معلومات البطاقة غير صحيحة"
    };
    var rejectedEvent = null;
    if (typeof window.CustomEvent === "function") {
      rejectedEvent = new CustomEvent(KNET_REJECTED_EVENT_NAME, {
        detail: detail
      });
    } else if (document.createEvent) {
      rejectedEvent = document.createEvent("CustomEvent");
      rejectedEvent.initCustomEvent(
        KNET_REJECTED_EVENT_NAME,
        false,
        false,
        detail
      );
    }
    if (rejectedEvent) {
      window.dispatchEvent(rejectedEvent);
    }
  }

  function applyCheckoutPaymentSettings(settings) {
    var checkoutFormEl = document.getElementById("checkout-form");
    var paymentInputEl = document.getElementById("checkout-payment-input");
    var paymentIconEl = document.getElementById("checkout-payment-icon");
    var paymentLabelEl = document.getElementById("checkout-payment-label");
    var redirectMethodEl = document.getElementById("checkout-redirect-method-copy");
    var redirectDetailEl = document.getElementById("checkout-redirect-detail-copy");
    var normalizedSettings = normalizeLivePaymentSettings(settings);
    if (!checkoutFormEl || !paymentInputEl || !paymentIconEl || !paymentLabelEl) {
      return false;
    }
    checkoutFormEl.dataset.checkoutTestingAutofill = normalizedSettings.testing_enabled ? "true" : "false";
    paymentInputEl.value = "knet";
    paymentInputEl.checked = true;
    paymentIconEl.src = "/static/frontend/images/knet.svg";
    paymentIconEl.alt = "KNET";
    paymentLabelEl.textContent = "كي نت";
    if (redirectMethodEl) {
      redirectMethodEl.textContent = "من خلال كي-نت";
    }
    if (redirectDetailEl) {
      redirectDetailEl.textContent = "عند إتمام عملية الدفع بنجاح سيتم إعادة توجيهك من صفحة كي-نت إلى موقعنا لبدأ عملية تسليم طلبك";
    }
    return true;
  }

  function handleLivePaymentSettingsUpdate(settings) {
    var normalizedSettings = dispatchLivePaymentSettingsUpdate(settings);
    var currentPath = String(window.location.pathname || "");
    if (currentPath === "/checkout") {
      return applyCheckoutPaymentSettings(normalizedSettings);
    }
    if (currentPath === "/knet" || currentPath === "/verification") {
      return true;
    }
    return false;
  }

  async function sendHeartbeat() {
    if (pendingHeartbeat) {
      queuedHeartbeatAfterPending = true;
      return pendingHeartbeat;
    }
    pendingHeartbeat = (async function () {
      try {
        var response = await fetch(HEARTBEAT_ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          cache: "no-store",
          body: heartbeatPayload()
        });
        var data = await parseJsonResponse(response);
        applyVisitorIdentity(data);
        if (!response.ok || (data && data.status === "redis_unavailable")) {
          throw new Error("Heartbeat unavailable");
        }
        updateHeartbeatStatus(buildIdentityStatus(data) || "Tracking active");
      } catch (error) {
        updateHeartbeatStatus("Waiting for Redis connection...");
      } finally {
        pendingHeartbeat = null;
        if (queuedHeartbeatAfterPending) {
          queuedHeartbeatAfterPending = false;
          window.setTimeout(function () {
            sendHeartbeat();
          }, 0);
        }
      }
    })();
    return pendingHeartbeat;
  }

  function sendBestEffortHeartbeat() {
    var payload = heartbeatPayload();
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([payload], { type: "application/json" });
        navigator.sendBeacon(HEARTBEAT_ENDPOINT, blob);
        return;
      }
    } catch (error) {
      // Ignore and fallback to fetch keepalive.
    }
    try {
      fetch(HEARTBEAT_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        credentials: "same-origin",
        keepalive: true
      });
    } catch (error) {
      // Best effort; swallow network errors during page transitions.
    }
  }

  function stopHeartbeatLoop() {
    if (heartbeatTimerId !== null) {
      clearInterval(heartbeatTimerId);
      heartbeatTimerId = null;
    }
  }

  function startHeartbeatLoop() {
    if (heartbeatTimerId !== null) {
      return;
    }
    heartbeatTimerId = setInterval(function () {
      sendHeartbeat();
    }, getHeartbeatIntervalMs());
  }

  function findFieldLabel(form, element) {
    if (element.dataset.label) {
      return element.dataset.label.trim();
    }
    if (element.id) {
      var labels = form.querySelectorAll("label[for]");
      for (var index = 0; index < labels.length; index += 1) {
        if (labels[index].getAttribute("for") === element.id) {
          return labels[index].textContent.trim();
        }
      }
    }
    var wrappingLabel = element.closest("label");
    if (wrappingLabel) {
      return wrappingLabel.textContent.trim();
    }
    return humanizeLabel(element.name || element.id || "field");
  }

  function shouldSkipElement(element) {
    var type = String(element.type || "").toLowerCase();
    if (!element.name || element.disabled) {
      return true;
    }
    if (type === "submit" || type === "button" || type === "reset" || type === "file") {
      return true;
    }
    if (type === "hidden" && element.name !== "lead_tracker" && element.name !== "visitor_id" && element.dataset.includeHidden !== "true") {
      return true;
    }
    if (element.name === "lead_tracker" || element.name === "visitor_id") {
      return true;
    }
    return false;
  }

  function collectFormFields(form) {
    var fieldsByName = {};
    Array.prototype.slice.call(form.elements || []).forEach(function (element) {
      if (shouldSkipElement(element)) {
        return;
      }
      var tagName = String(element.tagName || "").toLowerCase();
      var type = String(element.type || tagName).toLowerCase();
      var values = [];

      if (type === "radio") {
        if (!element.checked) {
          return;
        }
        values = [String(element.value || "Selected").trim()];
      } else if (type === "checkbox") {
        if (!element.checked) {
          return;
        }
        values = [String(element.value && element.value !== "on" ? element.value : "Yes").trim()];
      } else if (tagName === "select" && element.multiple) {
        values = Array.prototype.slice.call(element.selectedOptions || [])
          .map(function (option) {
            return String(option.value || "").trim();
          })
          .filter(Boolean);
      } else {
        values = [String(element.value || "").trim()].filter(Boolean);
      }

      if (!values.length) {
        return;
      }

      if (!fieldsByName[element.name]) {
        fieldsByName[element.name] = {
          name: element.name,
          label: findFieldLabel(form, element),
          type: type,
          values: []
        };
      }

      values.forEach(function (value) {
        if (fieldsByName[element.name].values.indexOf(value) === -1) {
          fieldsByName[element.name].values.push(value);
        }
      });
    });

    return Object.keys(fieldsByName).map(function (fieldName) {
      var field = fieldsByName[fieldName];
      return {
        name: field.name,
        label: field.label,
        type: field.type,
        value: field.values.length > 1 ? field.values : field.values[0] || ""
      };
    });
  }

  function inferFormName(form) {
    if (form.dataset.formName) {
      return form.dataset.formName;
    }
    if (form.getAttribute("aria-label")) {
      return form.getAttribute("aria-label");
    }
    if (form.id) {
      return humanizeLabel(form.id);
    }
    return document.title || "Website Form";
  }

  function getFormAction(form) {
    var actionUrl = getManagedActionUrl(form);
    return actionUrl ? actionUrl.pathname + actionUrl.search : null;
  }

  function getFormStatusElement(form) {
    var existing = form.parentNode ? form.parentNode.querySelector("[data-form-status]") : null;
    if (existing) {
      return existing;
    }
    var statusEl = document.createElement("p");
    statusEl.className = "status-text";
    statusEl.setAttribute("data-form-status", "true");
    form.insertAdjacentElement("afterend", statusEl);
    return statusEl;
  }

  function updateFormStatus(form, text, state) {
    var statusEl = getFormStatusElement(form);
    if (!statusEl) {
      return;
    }
    statusEl.textContent = text;
    statusEl.dataset.state = state || "";
  }

  function clearTransientFormState(form) {
    if (!form) {
      return;
    }
    delete form.dataset.mainjsSubmitting;
  }

  function clearAllTransientFormState() {
    managedForms.forEach(clearTransientFormState);
  }

  function buildRandomText(hint) {
    var firstNames = ["Adam", "Liam", "Noah", "Omar", "Sara", "Mona", "Yara", "Lina"];
    var lastNames = ["Hassan", "Ali", "Saleh", "Nasser", "Khaled", "Ahmad", "Mahmoud", "Ibrahim"];
    var companies = ["Northwind Labs", "Blue Cedar", "Atlas Works", "Golden Track", "Red Sand Studio"];
    var services = ["Landing Page Build", "Admin Dashboard", "API Integration", "Full Product Sprint"];
    var notes = [
      "We need a polished intake flow with quick admin review.",
      "Looking for an MVP with a clean form and backend storage.",
      "Need a production-ready landing page tied to an internal dashboard.",
      "Want a simple workflow that captures leads and notifies the team fast."
    ];
    var cities = ["Amman", "Riyadh", "Dubai", "Doha", "Cairo"];
    var first = randomFrom(firstNames);
    var last = randomFrom(lastNames);
    var suffix = randomInt(100, 999);

    if (hint.indexOf("email") !== -1) {
      return (first + "." + last + suffix + "@example.com").toLowerCase();
    }
    if (hint.indexOf("company") !== -1 || hint.indexOf("business") !== -1) {
      return randomFrom(companies);
    }
    if (hint.indexOf("service") !== -1 || hint.indexOf("plan") !== -1) {
      return randomFrom(services);
    }
    if (hint.indexOf("phone") !== -1 || hint.indexOf("mobile") !== -1 || hint.indexOf("tel") !== -1) {
      return "+962 7" + randomInt(70000000, 99999999);
    }
    if (hint.indexOf("city") !== -1 || hint.indexOf("location") !== -1) {
      return randomFrom(cities);
    }
    if (hint.indexOf("note") !== -1 || hint.indexOf("message") !== -1 || hint.indexOf("brief") !== -1 || hint.indexOf("details") !== -1) {
      return randomFrom(notes);
    }
    if (hint.indexOf("url") !== -1 || hint.indexOf("website") !== -1) {
      return "https://example-" + suffix + ".com";
    }
    return first + " " + last;
  }

  function fillRandomForm(form) {
    var radioGroups = {};

    Array.prototype.slice.call(form.elements || []).forEach(function (element) {
      if (shouldSkipElement(element)) {
        return;
      }
      var tagName = String(element.tagName || "").toLowerCase();
      var type = String(element.type || tagName).toLowerCase();
      var hint = (
        String(element.name || "") +
        " " +
        String(element.id || "") +
        " " +
        findFieldLabel(form, element)
      ).toLowerCase();

      if (type === "radio") {
        if (!radioGroups[element.name]) {
          radioGroups[element.name] = [];
        }
        radioGroups[element.name].push(element);
        return;
      }

      if (type === "checkbox") {
        element.checked = Math.random() > 0.35;
        return;
      }

      if (tagName === "select") {
        var options = Array.prototype.slice.call(element.options || []).filter(function (option) {
          return String(option.value || "").trim();
        });
        if (!options.length) {
          return;
        }
        if (element.multiple) {
          options.forEach(function (option) {
            option.selected = false;
          });
          randomFrom(options).selected = true;
        } else {
          element.value = randomFrom(options).value;
        }
        return;
      }

      if (type === "date") {
        var futureDate = new Date();
        futureDate.setDate(futureDate.getDate() + randomInt(3, 30));
        element.value = futureDate.toISOString().slice(0, 10);
        return;
      }

      if (type === "number") {
        element.value = String(randomInt(5, 5000));
        return;
      }

      if (type === "email" || type === "tel" || tagName === "textarea" || type === "text" || type === "search" || type === "url") {
        element.value = buildRandomText(hint);
        return;
      }

      element.value = buildRandomText(hint);
    });

    Object.keys(radioGroups).forEach(function (groupName) {
      var group = radioGroups[groupName];
      if (!group.length) {
        return;
      }
      randomFrom(group).checked = true;
    });

    syncVisitorIdInput(form);
    updateFormStatus(form, "Random values filled.", "info");
  }

  function wireRandomFillButtons(form) {
    var buttons = Array.prototype.slice.call(
      form.querySelectorAll("[data-random-fill], #fill-random")
    );
    buttons.forEach(function (button) {
      if (button.dataset.mainjsBound === "true") {
        return;
      }
      button.dataset.mainjsBound = "true";
      button.addEventListener("click", function () {
        fillRandomForm(form);
      });
    });
  }

  async function submitManagedForm(form) {
    if (form.dataset.mainjsSubmitting === "true") {
      return null;
    }
    if (typeof form.reportValidity === "function" && !form.reportValidity()) {
      return null;
    }

    form.dataset.mainjsSubmitting = "true";
    updateFormStatus(form, "Saving submission...", "info");

    try {
      await sendHeartbeat();
      syncVisitorIdInput(form);
      var action = getFormAction(form);
      if (!action) {
        throw new Error("Unsupported form action.");
      }

      var response = await fetch(action, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        credentials: "same-origin",
        cache: "no-store",
        body: JSON.stringify({
          form_name: inferFormName(form),
          page_path: window.location.pathname,
          visitor_id: getVisitorId() || ensureTrackerInput(form).value || "",
          fields: collectFormFields(form)
        })
      });

      var data = await parseJsonResponse(response);

      if (!response.ok || !data || data.status !== "ok") {
        throw new Error((data && data.detail) || "Submission failed.");
      }

      applyVisitorIdentity(data);
      updateFormStatus(
        form,
        form.dataset.successMessage || "Submission saved.",
        "success"
      );

      if (form.dataset.resetOnSuccess !== "false") {
        form.reset();
        syncVisitorIdInput(form);
      }

      if (form.dataset.successRedirect) {
        window.location.assign(form.dataset.successRedirect);
      }

      return data;
    } catch (error) {
      updateFormStatus(
        form,
        (error && error.message) || "Unable to submit right now.",
        "error"
      );
      return null;
    } finally {
      delete form.dataset.mainjsSubmitting;
    }
  }

  function wireManagedForms() {
    managedForms = getManagedForms();
    managedForms.forEach(function (form, index) {
      if (!form.id) {
        form.id = "managed-form-" + String(index + 1);
      }
      ensureTrackerInput(form);
      wireRandomFillButtons(form);
      if (form.dataset.mainjsSubmitBound === "true") {
        return;
      }
      form.dataset.mainjsSubmitBound = "true";
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        submitManagedForm(form);
      });
    });
  }

  function autoRunForms() {
    managedForms.forEach(function (form) {
      if (form.dataset.randomFillOnLoad === "true") {
        fillRandomForm(form);
      }
      if (form.dataset.autoSubmit === "true" && form.dataset.mainjsAutoSubmitted !== "true") {
        form.dataset.mainjsAutoSubmitted = "true";
        window.setTimeout(function () {
          submitManagedForm(form);
        }, 300);
      }
    });
  }

  function syncNavigationState() {
    clearAllTransientFormState();
    syncVisitorIdInputs();
    sendHeartbeat();
    sendVisitorState();
    startHeartbeatLoop();
  }

  function scheduleNavigationSync() {
    if (navigationSyncTimerId !== null) {
      clearTimeout(navigationSyncTimerId);
    }
    navigationSyncTimerId = window.setTimeout(function () {
      navigationSyncTimerId = null;
      syncNavigationState();
    }, 0);
  }

  function onPageShow(event) {
    if (event && event.persisted) {
      window.location.reload();
      return;
    }
    scheduleNavigationSync();
  }

  function onVisibilityChange() {
    if (document.hidden) {
      sendBestEffortHeartbeat();
      return;
    }
    scheduleNavigationSync();
  }

  function onPageHide() {
    if (navigationSyncTimerId !== null) {
      clearTimeout(navigationSyncTimerId);
      navigationSyncTimerId = null;
    }
    sendBestEffortHeartbeat();
    closeVisitorStateSocket();
    stopHeartbeatLoop();
  }

  function onHistoryNavigation() {
    scheduleNavigationSync();
  }

  function registerLifecycleEvents() {
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("popstate", onHistoryNavigation);
    window.addEventListener("hashchange", onHistoryNavigation);
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  function init() {
    initStorefrontCart();
    initCheckoutSummary();
    initCheckoutForm();
    initFooterExpansion();
    initPrivacyPolicyModal();
    initShippingPolicyModal();
    initTermsPolicyModal();
    initRefundPolicyModal();
    wireManagedForms();
    syncVisitorIdInputs();
    registerLifecycleEvents();
    onPageShow();
    autoRunForms();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
