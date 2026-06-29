const colors = ["#0d7c80", "#ec6b5b", "#5962a6", "#3b7a57", "#f2b84b", "#8b5f2e"];
let map = null;
let markers = [];
let lines = [];
let latestPoints = [];

const form = document.getElementById("plannerForm");
const submitButton = document.getElementById("submitButton");
const statusPanel = document.getElementById("status");
const result = document.getElementById("result");
const mapTitle = document.getElementById("mapTitle");
const routeList = document.getElementById("routeList");
const selectedPlace = document.getElementById("selectedPlace");
const overlapPanel = document.getElementById("overlapPanel");
const planOutput = document.getElementById("planOutput");
const scheduleBoard = document.getElementById("scheduleBoard");
const progressPanel = document.getElementById("progressPanel");
const progressBar = document.getElementById("progressBar");
const progressLabel = document.getElementById("progressLabel");
const progressPercent = document.getElementById("progressPercent");
const wizardSteps = [...document.querySelectorAll(".wizard-step")];
const wizardDots = [...document.querySelectorAll(".wizard-progress span")];
const wizardTitle = document.getElementById("wizardTitle");
const wizardHint = document.getElementById("wizardHint");
const wizardStepText = document.getElementById("wizardStepText");
const prevStepButton = document.getElementById("prevStep");
const nextStepButton = document.getElementById("nextStep");
const daysInput = document.getElementById("days");
const startDateInput = document.getElementById("startDate");
const endDateInput = document.getElementById("endDate");
const tripDaysInput = document.getElementById("tripDays");
const calendarYearSelect = document.getElementById("calendarYear");
const calendarMonthSelect = document.getElementById("calendarMonth");
const calendarGrid = document.getElementById("calendarGrid");
const dateSummary = document.getElementById("dateSummary");
let progressTimer = null;
let currentWizardStep = 0;
const today = new Date();
let calendarViewYear = today.getFullYear();
let calendarViewMonth = today.getMonth();
let selectedStartDate = "";
let selectedEndDate = "";

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatWon(value) {
  const number = Number(value || 0);
  if (!number) return "무료";
  return `${number.toLocaleString("ko-KR")}원`;
}

function isWalkStep(step) {
  if (!step) return false;
  const instruction = String(step.public_transport?.instruction || "");
  return step.recommended_mode === "walk" || (Number(step.public_transport?.fare_krw || 0) === 0 && /도보|걷/.test(instruction));
}

function publicMoveLabel(step) {
  if (isWalkStep(step)) {
    return `도보 추천 ${step.public_transport.duration_minutes}분`;
  }
  return `대중교통 ${step.public_transport.duration_minutes}분`;
}

function moveTooltip(step) {
  if (isWalkStep(step)) {
    return `도보 ${step.public_transport.duration_minutes}분 · 택시 ${step.taxi.duration_minutes}분`;
  }
  return `대중교통 ${step.public_transport.duration_minutes}분 · 택시 ${step.taxi.duration_minutes}분`;
}

function showStatus(message, type = "info") {
  statusPanel.textContent = message;
  statusPanel.className = `status-panel ${type === "error" ? "error" : ""}`;
}

function hideStatus() {
  statusPanel.className = "status-panel hidden";
  statusPanel.textContent = "";
}

function dateKey(year, monthIndex, day) {
  return `${year}-${String(monthIndex + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function parseDateKey(key) {
  const [year, month, day] = String(key || "").split("-").map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day);
}

function formatKoreanDate(key) {
  const parsed = parseDateKey(key);
  if (!parsed) return "";
  return `${parsed.getFullYear()}년 ${parsed.getMonth() + 1}월 ${parsed.getDate()}일`;
}

function selectedTripLength() {
  const start = parseDateKey(selectedStartDate);
  const end = parseDateKey(selectedEndDate);
  if (!start || !end) return 0;
  return Math.max(1, Math.round((end - start) / 86400000) + 1);
}

function updateDateInputs() {
  const totalDays = selectedTripLength();
  startDateInput.value = selectedStartDate;
  endDateInput.value = selectedEndDate;
  tripDaysInput.value = totalDays ? String(totalDays) : "";

  if (!selectedStartDate) {
    daysInput.value = "";
    dateSummary.innerHTML = `
      <span>여행 기간</span>
      <strong>날짜 선택 전</strong>
      <small>출발일과 돌아오는 날을 선택하세요.</small>
    `;
    return;
  }

  if (!selectedEndDate) {
    daysInput.value = "";
    dateSummary.innerHTML = `
      <span>출발일</span>
      <strong>${escapeHtml(formatKoreanDate(selectedStartDate))}</strong>
      <small>돌아오는 날을 선택하세요. 당일 여행이면 같은 날짜를 한 번 더 누르세요.</small>
    `;
    return;
  }

  const nights = Math.max(0, totalDays - 1);
  daysInput.value = `${nights}박 ${totalDays}일`;
  dateSummary.innerHTML = `
    <span>계산된 기간</span>
    <strong>${escapeHtml(daysInput.value)}</strong>
    <small>${escapeHtml(formatKoreanDate(selectedStartDate))} → ${escapeHtml(formatKoreanDate(selectedEndDate))}</small>
  `;
}

function isDateInSelectedRange(key) {
  return Boolean(selectedStartDate && selectedEndDate && key > selectedStartDate && key < selectedEndDate);
}

function chooseCalendarDate(key) {
  if (!selectedStartDate || selectedEndDate || key < selectedStartDate) {
    selectedStartDate = key;
    selectedEndDate = "";
  } else {
    selectedEndDate = key;
  }
  updateDateInputs();
  renderCalendar();
  hideStatus();
}

function renderCalendar() {
  if (!calendarGrid) return;
  const firstDay = new Date(calendarViewYear, calendarViewMonth, 1).getDay();
  const lastDate = new Date(calendarViewYear, calendarViewMonth + 1, 0).getDate();
  const totalCells = Math.ceil((firstDay + lastDate) / 7) * 7;
  const todayKey = dateKey(today.getFullYear(), today.getMonth(), today.getDate());
  const cells = [];

  for (let cell = 0; cell < totalCells; cell += 1) {
    const day = cell - firstDay + 1;
    if (day < 1 || day > lastDate) {
      cells.push('<span class="calendar-empty"></span>');
      continue;
    }

    const key = dateKey(calendarViewYear, calendarViewMonth, day);
    const classes = ["calendar-day"];
    if (key === todayKey) classes.push("today");
    if (key === selectedStartDate) classes.push("selected-start");
    if (key === selectedEndDate) classes.push("selected-end");
    if (isDateInSelectedRange(key)) classes.push("in-range");
    cells.push(`<button type="button" class="${classes.join(" ")}" data-date="${key}">${day}</button>`);
  }

  calendarGrid.innerHTML = cells.join("");
  calendarGrid.querySelectorAll("button[data-date]").forEach((button) => {
    button.addEventListener("click", () => chooseCalendarDate(button.dataset.date));
  });
}

function initializeCalendar() {
  if (!calendarYearSelect || !calendarMonthSelect) return;
  const currentYear = today.getFullYear();
  calendarYearSelect.innerHTML = "";
  for (let year = 2000; year <= currentYear + 5; year += 1) {
    const option = document.createElement("option");
    option.value = String(year);
    option.textContent = `${year}년`;
    option.selected = year === calendarViewYear;
    calendarYearSelect.appendChild(option);
  }

  calendarMonthSelect.innerHTML = "";
  for (let month = 0; month < 12; month += 1) {
    const option = document.createElement("option");
    option.value = String(month);
    option.textContent = `${month + 1}월`;
    option.selected = month === calendarViewMonth;
    calendarMonthSelect.appendChild(option);
  }

  calendarYearSelect.addEventListener("change", () => {
    calendarViewYear = Number(calendarYearSelect.value);
    renderCalendar();
  });
  calendarMonthSelect.addEventListener("change", () => {
    calendarViewMonth = Number(calendarMonthSelect.value);
    renderCalendar();
  });

  updateDateInputs();
  renderCalendar();
}

function updateWizard() {
  wizardSteps.forEach((step, index) => {
    step.classList.toggle("active", index === currentWizardStep);
  });
  wizardDots.forEach((dot, index) => {
    dot.classList.toggle("active", index <= currentWizardStep);
  });

  const step = wizardSteps[currentWizardStep];
  wizardTitle.textContent = step?.dataset.title || "여행 조건";
  wizardHint.textContent = step?.dataset.hint || "";
  wizardStepText.textContent = `${currentWizardStep + 1} / ${wizardSteps.length}`;
  const isFinalStep = currentWizardStep === wizardSteps.length - 1;
  prevStepButton.classList.toggle("hidden", currentWizardStep === 0 || isFinalStep);
  nextStepButton.classList.toggle("hidden", isFinalStep);
}

function validateWizardStep() {
  const step = wizardSteps[currentWizardStep];
  const required = [...step.querySelectorAll("[required]")];
  for (const field of required) {
    if (!field.value.trim()) {
      if (field.id === "days") {
        calendarGrid?.focus();
        showStatus("여행 출발일과 돌아오는 날을 달력에서 선택해주세요.", "error");
      } else {
        field.focus();
        showStatus("현재 질문을 먼저 입력해주세요.", "error");
      }
      return false;
    }
  }
  hideStatus();
  return true;
}

prevStepButton.addEventListener("click", () => {
  currentWizardStep = Math.max(0, currentWizardStep - 1);
  updateWizard();
  hideStatus();
});

nextStepButton.addEventListener("click", () => {
  if (!validateWizardStep()) return;
  currentWizardStep = Math.min(wizardSteps.length - 1, currentWizardStep + 1);
  updateWizard();
});

initializeCalendar();
updateWizard();

function setProgress(percent, label) {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  progressPanel.classList.remove("hidden");
  progressBar.style.width = `${safePercent}%`;
  progressPercent.textContent = `${safePercent}%`;
  progressLabel.textContent = label;
  if (submitButton.disabled) {
    submitButton.textContent = `일정 생성 중 ${safePercent}%`;
  }
}

function startProgress() {
  window.clearInterval(progressTimer);
  const stages = [
    [12, "조건 정리 중"],
    [28, "방문지 후보 찾는 중"],
    [45, "날짜별 동선 구성 중"],
    [63, "지도 좌표 확인 중"],
    [78, "이동 시간 계산 중"],
    [90, "화면 구성 중"],
    [96, "마지막 검토 중"],
    [98, "곧 완성됩니다"],
  ];
  let stageIndex = 0;
  setProgress(5, "일정 준비 중");

  progressTimer = window.setInterval(() => {
    const current = Number(progressPercent.textContent.replace("%", "")) || 0;
    const nextStage = stages[stageIndex] || [98, "곧 완성됩니다"];
    const target = nextStage[0];
    const step = current < 60 ? 4 : 2;
    const next = Math.min(target, current + step);
    setProgress(next, nextStage[1]);
    if (next >= target && stageIndex < stages.length - 1) {
      stageIndex += 1;
    }
  }, 520);
}

function finishProgress(label = "완성") {
  window.clearInterval(progressTimer);
  setProgress(100, label);
  window.setTimeout(() => {
    progressPanel.classList.add("hidden");
    progressBar.style.width = "0%";
  }, 850);
}

function stopProgress(label = "중단됨") {
  window.clearInterval(progressTimer);
  setProgress(100, label);
  submitButton.textContent = "여행 일정 생성";
}

async function requestPlan(payload) {
  const response = await fetch("api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (response.status === 404 || response.status === 405) {
    const staticModeError = new Error("정적 데모 모드");
    staticModeError.staticMode = true;
    throw staticModeError;
  }

  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    throw new Error("서버 응답을 JSON으로 읽지 못했습니다.");
  }

  if (!response.ok) {
    throw new Error(data.error || "일정 생성 중 오류가 발생했습니다.");
  }
  return data;
}

function collectPayload() {
  const data = new FormData(form);
  const style = [...document.querySelectorAll('input[name="style"]:checked')].map((item) => item.value);
  return {
    region: data.get("region"),
    budget: data.get("budget"),
    days: data.get("days"),
    start_date: data.get("start_date"),
    end_date: data.get("end_date"),
    trip_days: Number(data.get("trip_days")) || undefined,
    companion: data.get("companion"),
    transport: data.get("transport"),
    live_groq:
      data.get("liveGroq") === "on" ||
      new URLSearchParams(window.location.search).get("live") === "1",
    style,
  };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (currentWizardStep !== wizardSteps.length - 1) {
    if (validateWizardStep()) {
      currentWizardStep = Math.min(wizardSteps.length - 1, currentWizardStep + 1);
      updateWizard();
    }
    return;
  }
  const payload = collectPayload();
  submitButton.disabled = true;
  submitButton.textContent = "일정 생성 중 0%";
  startProgress();
  hideStatus();

  try {
    const data = await requestPlan(payload);
    finishProgress("일정 완성");
    hideStatus();
    renderResult(data, payload.region);
  } catch (error) {
    if (error.staticMode || error instanceof TypeError) {
      finishProgress("데모 표시 완료");
      renderResult(createDemoPlan(payload), payload.region);
      showStatus("정적 데모 모드입니다. 실제 AI 생성은 Render 배포 링크에서 동작합니다.");
    } else {
      stopProgress("오류 발생");
      showStatus(error.message, "error");
    }
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "여행 일정 생성";
  }
});

function createDemoPlan(payload) {
  const region = payload.region || "부산";
  const points = [
    {
      id: "demo-1-1",
      day: 1,
      order: 1,
      time: "10:00",
      place: `${region}역`,
      address: `${region} 중심 교통 거점`,
      lat: 35.1151,
      lng: 129.0415,
      category: "이동",
      summary: "여행을 시작하기 좋은 교통 거점입니다.",
      tip: "도착 직후 교통카드와 짐 보관 여부를 확인하세요.",
      location_source: "데모 좌표",
    },
    {
      id: "demo-1-2",
      day: 1,
      order: 2,
      time: "11:00",
      place: "감천문화마을",
      address: "부산광역시 사하구 감내2로 203",
      lat: 35.0975,
      lng: 129.0106,
      category: "관광지",
      summary: "계단식 마을과 벽화 골목이 이어지는 대표 산책 코스입니다.",
      tip: "오르막이 많아서 편한 신발을 추천합니다.",
      location_source: "데모 좌표",
    },
    {
      id: "demo-1-3",
      day: 1,
      order: 3,
      time: "13:00",
      place: "자갈치시장",
      address: "부산광역시 중구 자갈치해안로 52",
      lat: 35.0969,
      lng: 129.0305,
      category: "맛집",
      summary: "부산 해산물 분위기를 가장 쉽게 느낄 수 있는 시장입니다.",
      tip: "점심 피크 시간에는 대기 시간을 넉넉히 잡으세요.",
      location_source: "데모 좌표",
    },
    {
      id: "demo-1-4",
      day: 1,
      order: 4,
      time: "15:00",
      place: "흰여울문화마을",
      address: "부산광역시 영도구 영선동4가",
      lat: 35.0789,
      lng: 129.0447,
      category: "카페",
      summary: "바다를 따라 걷는 골목과 카페가 좋은 오후 코스입니다.",
      tip: "바람이 강할 수 있어 겉옷을 준비하면 좋습니다.",
      location_source: "데모 좌표",
    },
    {
      id: "demo-2-1",
      day: 2,
      order: 1,
      time: "09:30",
      place: "해운대해수욕장",
      address: "부산광역시 해운대구 우동",
      lat: 35.1587,
      lng: 129.1604,
      category: "자연",
      summary: "부산의 대표 바다 풍경을 보는 산책 코스입니다.",
      tip: "아침 시간대가 비교적 한산합니다.",
      location_source: "데모 좌표",
    },
    {
      id: "demo-2-2",
      day: 2,
      order: 2,
      time: "11:00",
      place: "동백섬",
      address: "부산광역시 해운대구 우동 710-1",
      lat: 35.1532,
      lng: 129.1516,
      category: "자연",
      summary: "해운대와 광안대교 전망을 함께 보기 좋은 산책지입니다.",
      tip: "해변에서 도보 이동이 편합니다.",
      location_source: "데모 좌표",
    },
    {
      id: "demo-2-3",
      day: 2,
      order: 3,
      time: "13:00",
      place: "센텀시티",
      address: "부산광역시 해운대구 센텀남대로 35",
      lat: 35.1693,
      lng: 129.1308,
      category: "문화",
      summary: "쇼핑과 실내 휴식을 같이 넣기 좋은 코스입니다.",
      tip: "비 오는 날 대체 일정으로도 좋습니다.",
      location_source: "데모 좌표",
    },
  ];

  const steps = [
    demoStep(1, 1, 2, "부산역", "감천문화마을", 9.2, "버스 87번 또는 1011번 연계", 35, 1550, 24, 13000),
    demoStep(1, 2, 3, "감천문화마을", "자갈치시장", 4.1, "마을버스 환승 후 지하철 1호선 연계", 24, 1550, 14, 8500),
    demoStep(1, 3, 4, "자갈치시장", "흰여울문화마을", 4.8, "버스 6번 또는 9번 영도 방향", 28, 1550, 16, 9000),
    demoStep(2, 1, 2, "해운대해수욕장", "동백섬", 1.4, "해변 산책로 도보 이동", 18, 0, 6, 5000),
    demoStep(2, 2, 3, "동백섬", "센텀시티", 4.0, "버스 1001번 또는 지하철 2호선 연계", 22, 1550, 12, 7600),
  ];

  return {
    plan_markdown: `## ${region} 데모 여행 일정\n\n| 날짜 | 시간 | 장소 | 다음 이동 |\n|---|---:|---|---|\n| 1일차 | 10:00 | ${region}역 | 감천문화마을까지 대중교통 약 35분 |\n| 1일차 | 11:00 | 감천문화마을 | 자갈치시장까지 대중교통 약 24분 |\n| 1일차 | 13:00 | 자갈치시장 | 흰여울문화마을까지 대중교통 약 28분 |\n| 2일차 | 09:30 | 해운대해수욕장 | 동백섬까지 도보 약 18분 |\n| 2일차 | 11:00 | 동백섬 | 센텀시티까지 대중교통 약 22분 |\n\n이 화면은 GitHub Pages 발표용 정적 데모입니다. 실제 AI 생성은 서버 배포 버전에서 동작합니다.`,
    route_points: points,
    transport_steps: steps,
    meta: { model: "static-demo", route_count: points.length, transport_step_count: steps.length },
  };
}

function demoStep(day, fromOrder, toOrder, fromPlace, toPlace, distance, instruction, publicMinutes, publicFare, taxiMinutes, taxiFare) {
  const walk = Number(publicFare || 0) === 0 || /도보|산책/.test(instruction);
  return {
    day,
    from_order: fromOrder,
    to_order: toOrder,
    from_place: fromPlace,
    to_place: toPlace,
    distance_km: distance,
    recommended_mode: walk ? "walk" : "public",
    walk: {
      duration_minutes: publicMinutes,
      instruction: `도보 이용 추천. 약 ${publicMinutes}분 소요`,
    },
    public_transport: {
      instruction: walk ? `도보 이용 추천. 약 ${publicMinutes}분 소요` : instruction,
      duration_minutes: publicMinutes,
      fare_krw: publicFare,
      note: "발표용 예시 정보입니다.",
    },
    taxi: {
      duration_minutes: taxiMinutes,
      fare_krw: taxiFare,
      note: "교통 상황에 따라 달라질 수 있습니다.",
    },
  };
}

function renderResult(data, region) {
  result.classList.remove("hidden");
  mapTitle.textContent = `${region} 동선 지도`;
  latestPoints = data.route_points || [];
  renderSchedule(latestPoints, data.transport_steps || []);
  renderMap(latestPoints, data.transport_steps || []);
  renderRouteList(latestPoints);
  renderPlan(data.plan_markdown || "");
  if (data.meta?.warning) {
    showStatus(data.meta.warning);
  }
  result.scrollIntoView({ behavior: "smooth", block: "start" });
}

function ensureMap() {
  if (map) {
    markers.forEach((marker) => marker.remove());
    lines.forEach((line) => line.remove());
    markers = [];
    lines = [];
    return;
  }

  map = L.map("map", {
    scrollWheelZoom: true,
    zoomControl: true,
  });

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
}

function groupPoints(points) {
  const groups = new Map();
  points.forEach((point) => {
    const key = `${Number(point.lat).toFixed(5)},${Number(point.lng).toFixed(5)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(point);
  });
  return [...groups.values()];
}

function byDay(points) {
  const grouped = {};
  points.forEach((point) => {
    const day = Number(point.day) || 1;
    if (!grouped[day]) grouped[day] = [];
    grouped[day].push(point);
  });
  Object.keys(grouped).forEach((day) => {
    grouped[day].sort((a, b) => Number(a.order) - Number(b.order));
  });
  return grouped;
}

function renderMap(points, transportSteps) {
  ensureMap();
  overlapPanel.classList.add("hidden");

  if (!points.length) {
    map.setView([36.5, 127.8], 7);
    return;
  }

  const bounds = [];
  const groups = groupPoints(points);

  groups.forEach((group) => {
    const point = group[0];
    const day = Number(point.day) || 1;
    const color = colors[(day - 1) % colors.length];
    const isOverlap = group.length > 1;
    const label = isOverlap ? "!" : `${point.order}`;
    const marker = L.marker([point.lat, point.lng], {
      icon: L.divIcon({
        className: "route-marker",
        html: `<span class="${isOverlap ? "overlap" : ""}" style="--marker-color:${color}">${escapeHtml(label)}</span>`,
        iconSize: [36, 36],
        iconAnchor: [18, 18],
      }),
      riseOnHover: true,
    }).addTo(map);

    marker.on("mouseover", () => {
      showOverlapPanel(group);
      renderSelectedPlace(group[0]);
    });
    marker.on("click", () => {
      showOverlapPanel(group);
      renderSelectedPlace(group[0]);
    });

    markers.push(marker);
    bounds.push([point.lat, point.lng]);
  });

  const dayMap = byDay(points);
  const stepLookup = new Map(
    transportSteps.map((step) => [`${step.day}-${step.from_order}-${step.to_order}`, step])
  );

  Object.keys(dayMap).forEach((day) => {
    const color = colors[(Number(day) - 1) % colors.length];
    const dayPoints = dayMap[day];
    for (let index = 0; index < dayPoints.length - 1; index += 1) {
      const start = dayPoints[index];
      const end = dayPoints[index + 1];
      const step = stepLookup.get(`${day}-${start.order}-${end.order}`);
      const line = L.polyline([[start.lat, start.lng], [end.lat, end.lng]], {
        color,
        weight: 5,
        opacity: 0.82,
        lineCap: "round",
        lineJoin: "round",
      }).addTo(map);

      const move = step ? moveTooltip(step) : `${day}일차 ${index + 1}번 동선`;
      line.bindTooltip(move, {
        sticky: true,
        direction: "top",
        className: "route-tooltip",
      });

      line.on("mouseover", () => line.setStyle({ weight: 9, opacity: 1 }));
      line.on("mouseout", () => line.setStyle({ weight: 5, opacity: 0.82 }));
      lines.push(line);
    }
  });

  if (bounds.length === 1) {
    map.setView(bounds[0], 13);
  } else {
    map.fitBounds(bounds, { padding: [42, 42] });
  }
  setTimeout(() => map.invalidateSize(), 80);
}

function showOverlapPanel(group) {
  const isMultiple = group.length > 1;
  overlapPanel.innerHTML = `
    <h3>${isMultiple ? "겹친 장소 선택" : "장소 정보"}</h3>
    <p>${isMultiple ? "같은 위치에 여러 장소가 있습니다. 확인할 항목을 선택하세요." : "선택한 장소를 오른쪽에서 확인하세요."}</p>
    ${group
      .map(
        (point) => `
          <button class="choice-button" data-id="${escapeHtml(point.id)}">
            <strong>${escapeHtml(point.order)}. ${escapeHtml(point.place)}</strong><br />
            <span>${escapeHtml(point.time)} · ${escapeHtml(point.category)}</span>
          </button>
        `
      )
      .join("")}
  `;
  overlapPanel.classList.remove("hidden");
  overlapPanel.querySelectorAll(".choice-button").forEach((button) => {
    button.addEventListener("click", () => {
      const point = latestPoints.find((item) => item.id === button.dataset.id);
      if (point) renderSelectedPlace(point);
    });
  });
}

function renderSelectedPlace(point) {
  selectedPlace.innerHTML = `
    <h3>${escapeHtml(point.place)}</h3>
    <p><strong>${escapeHtml(point.time)} · ${escapeHtml(point.category)}</strong></p>
    <p>주소: ${escapeHtml(point.address || "주소 확인 필요")}</p>
    <p>특징: ${escapeHtml(point.summary)}</p>
    ${point.tip ? `<p>팁: ${escapeHtml(point.tip)}</p>` : ""}
    <p>좌표 출처: ${escapeHtml(point.location_source || "확인 필요")}</p>
  `;
}

function renderRouteList(points) {
  const dayMap = byDay(points);
  routeList.innerHTML = Object.keys(dayMap)
    .sort((a, b) => Number(a) - Number(b))
    .map((day) => {
      const color = colors[(Number(day) - 1) % colors.length];
      const items = dayMap[day]
        .map(
          (point) => `
            <div class="route-item" data-id="${escapeHtml(point.id)}">
              <span class="route-num" style="background:${color}">${escapeHtml(point.order)}</span>
              <div>
                <div class="route-place">${escapeHtml(point.place)}</div>
                <div class="route-meta">${escapeHtml(point.time)} · ${escapeHtml(point.category)}</div>
                <div class="route-meta">${escapeHtml(point.address || "주소 확인 필요")}</div>
                <div class="route-meta">${escapeHtml(point.summary)}</div>
              </div>
            </div>
          `
        )
        .join("");
      return `<div class="day-block"><div class="day-title"><span class="legend-dot" style="background:${color}"></span>${day}일차</div>${items}</div>`;
    })
    .join("");

  routeList.querySelectorAll(".route-item").forEach((item) => {
    item.addEventListener("click", () => {
      const point = latestPoints.find((value) => value.id === item.dataset.id);
      if (!point) return;
      renderSelectedPlace(point);
      map.setView([point.lat, point.lng], Math.max(map.getZoom(), 14));
    });
  });
}

function renderPrimaryTransport(step) {
  if (isWalkStep(step)) {
    return `
      <div class="transport-option walk-option">
        <span class="transport-label">도보 추천</span>
        <strong>${escapeHtml(step.public_transport.duration_minutes)}분</strong>
        <p>${escapeHtml(step.public_transport.instruction || "짧은 구간이라 걸어서 이동하기 좋습니다.")}</p>
      </div>
    `;
  }

  return `
    <div class="transport-option">
      <span class="transport-label">버스/지하철</span>
      <strong>${formatWon(step.public_transport.fare_krw)}</strong>
      <p>${escapeHtml(step.public_transport.instruction)}</p>
    </div>
  `;
}

function renderSchedule(points, steps) {
  if (!points.length) {
    scheduleBoard.innerHTML = "<p class='notice'>표시할 일정표가 아직 없습니다.</p>";
    return;
  }

  const dayMap = byDay(points);
  const stepLookup = new Map(
    steps.map((step) => [`${step.day}-${step.from_order}-${step.to_order}`, step])
  );

  scheduleBoard.innerHTML = Object.keys(dayMap)
    .sort((a, b) => Number(a) - Number(b))
    .map((day) => {
      const color = colors[(Number(day) - 1) % colors.length];
      const dayPoints = dayMap[day];
      const rows = dayPoints
        .map((point, index) => {
          const nextPoint = dayPoints[index + 1];
          const step = nextPoint ? stepLookup.get(`${day}-${point.order}-${nextPoint.order}`) : null;
          const moveHtml = step
            ? `
                <div class="move-card-head">
                  <span>다음 이동</span>
                  <strong>${escapeHtml(point.place)} → ${escapeHtml(nextPoint.place)}</strong>
                </div>
                <div class="move-chip-row">
                  <span class="move-chip">거리 ${escapeHtml(step.distance_km)}km</span>
                  <span class="move-chip">${escapeHtml(publicMoveLabel(step))}</span>
                  <span class="move-chip">택시 ${escapeHtml(step.taxi.duration_minutes)}분</span>
                </div>
                <div class="inline-move-grid">
                  ${renderPrimaryTransport(step)}
                  <div class="transport-option">
                    <span class="transport-label">택시</span>
                    <strong>${formatWon(step.taxi.fare_krw)}</strong>
                    <p>${escapeHtml(step.taxi.note)}</p>
                  </div>
                </div>
              `
            : `<div class="inline-move-end">이 날짜의 마지막 장소입니다.</div>`;

          return `
            <article class="schedule-row" data-id="${escapeHtml(point.id)}">
              <div class="schedule-place">
                <span class="schedule-num" style="background:${color}">${escapeHtml(point.order)}</span>
                <div>
                  <div class="schedule-time">
                    <strong>${escapeHtml(point.time)}</strong>
                    <span>${escapeHtml(point.category)}</span>
                  </div>
                  <h3>${escapeHtml(point.place)}</h3>
                  <p class="schedule-address">${escapeHtml(point.address || "주소 확인 필요")}</p>
                  <p class="schedule-summary">${escapeHtml(point.summary)}</p>
                </div>
              </div>
              <div class="schedule-move">${moveHtml}</div>
            </article>
          `;
        })
        .join("");
      return `<section class="schedule-day"><div class="day-title"><span class="legend-dot" style="background:${color}"></span>${day}일차</div>${rows}</section>`;
    })
    .join("");

  scheduleBoard.querySelectorAll(".schedule-row").forEach((row) => {
    row.addEventListener("click", () => {
      const point = latestPoints.find((value) => value.id === row.dataset.id);
      if (!point) return;
      renderSelectedPlace(point);
      if (map) {
        map.setView([point.lat, point.lng], Math.max(map.getZoom(), 14));
      }
    });
  });
}

function normalizeSectionTitle(value, index) {
  const cleaned = String(value || "")
    .replace(/^#+\s*/, "")
    .replace(/^\d+[.)]\s*/, "")
    .trim();
  return cleaned || (index === 0 ? "여행 요약" : `섹션 ${index + 1}`);
}

function splitMarkdownSections(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const sections = [];
  let current = { title: "여행 요약", body: [] };

  lines.forEach((line) => {
    const heading = line.match(/^\s{0,3}#{1,3}\s+(.+)$/);
    const numberedHeading = line.match(/^\s*\d+[.)]\s+([^|]+)\s*$/);
    if (heading || numberedHeading) {
      if (current.body.join("\n").trim()) {
        sections.push(current);
      }
      current = { title: normalizeSectionTitle((heading || numberedHeading)[1], sections.length), body: [] };
      return;
    }
    current.body.push(line);
  });

  if (current.body.join("\n").trim()) {
    sections.push(current);
  }

  return sections.length ? sections : [{ title: "여행 요약", body: [markdown] }];
}

function buildMarkdownPreview(markdown) {
  const content = String(markdown || "").trim();
  if (!content) return { preview: "", clipped: false };

  const lines = content.split(/\r?\n/);
  const picked = [];
  let visibleLines = 0;
  let tableRows = 0;

  lines.forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    if (trimmed.startsWith("|")) {
      tableRows += /^\|?\s*:?-/.test(trimmed) ? 0 : 1;
      if (tableRows <= 5 || /^\|?\s*:?-/.test(trimmed)) {
        picked.push(line);
      }
      visibleLines += 1;
      return;
    }

    if (visibleLines < 6) {
      picked.push(line);
    }
    visibleLines += 1;
  });

  return {
    preview: picked.join("\n"),
    clipped: visibleLines > 6 || tableRows > 5,
  };
}

function renderMarkdownFragment(markdown) {
  const content = String(markdown || "").trim();
  if (!content) return "<p>세부 내용이 없습니다.</p>";

  return window.marked ? sanitizeHtml(marked.parse(content)) : `<pre>${escapeHtml(content)}</pre>`;
}

function sanitizeHtml(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, iframe, object, embed, link, style").forEach((node) => node.remove());
  template.content.querySelectorAll("*").forEach((node) => {
    [...node.attributes].forEach((attribute) => {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim().toLowerCase();
      if (name.startsWith("on") || ((name === "href" || name === "src") && value.startsWith("javascript:"))) {
        node.removeAttribute(attribute.name);
      }
    });
  });
  return template.innerHTML;
}

function renderPlan(markdown) {
  const sections = splitMarkdownSections(markdown);
  planOutput.innerHTML = `
    <div class="plan-section-grid">
      ${sections
        .map((section, index) => {
          const isDaySection = /^\d+일차/.test(section.title);
          const isPairSection = /^(예상 비용표|준비물 체크리스트)$/.test(section.title);
          const isChecklistSection = section.title === "준비물 체크리스트";
          if (isDaySection) {
            return `
            <details class="plan-section tone-${index % 5}">
              <summary class="plan-section-head toggle-head">
                <span>${String(index + 1).padStart(2, "0")}</span>
                <h3>${escapeHtml(section.title)}</h3>
                <em>눌러서 상세 보기</em>
              </summary>
              <div class="plan-section-body">${renderMarkdownFragment(section.body.join("\n"))}</div>
            </details>
          `;
          }
          return `
            <article class="plan-section ${isPairSection ? "pair-section" : ""} ${isChecklistSection ? "checklist-section" : ""} tone-${index % 5}">
              <div class="plan-section-head">
                <span>${String(index + 1).padStart(2, "0")}</span>
                <h3>${escapeHtml(section.title)}</h3>
              </div>
              <div class="plan-section-body">${renderMarkdownFragment(section.body.join("\n"))}</div>
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}
