// Course Management UI against the dashboard API (mocked with MSW): summary, list filters,
// create/edit with IST dates and validity presets, content builder (add, reorder, delete),
// language versions, publishing, enrollments with their saved validity, taxonomy,
// viewer read-only access and accessibility.

import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { axeViolations } from "../test/a11y";
import { renderApp } from "../test/render";
import { CSRF, requests, server } from "../test/server";
import type { CourseDetail, Enrollment, Lecture } from "./courses/types";
import { paiseToRupees, rupeesToPaise } from "./courses/shared";
import { uploadToStorage } from "./courses/FileField";

const NOW = "2026-11-02T06:00:00Z";
const A = "*/api/v1/courses";
const last = (method: string, path: string) => requests.filter((r) => r.method === method && r.url.pathname === path).at(-1)!;
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 25, offset: 0 });

function lecture(over: Partial<Lecture> = {}): Lecture {
  return {
    id: 31, title: "Journal entries", description: null, thumbnail: null, duration_seconds: 1800, is_free_preview: false,
    status: "published", default_language: "english", position: 0, resources: [],
    variants: [{ id: 41, language: "english", language_name: "English", video: null, video_url: "https://cdn.example.com/english.mp4",
      audio: null, subtitle: null, thumbnail: null, description: null }],
    ...over,
  };
}

function course(over: Partial<CourseDetail> = {}): CourseDetail {
  return {
    id: 7, title: "Class 12 Accountancy", description: "Board syllabus", goals: null, thumbnail: null, class_id: 1, class_name: "Class 12",
    stream_id: 2, stream_name: "Commerce", subject_id: 3, subject_name: "Accountancy", is_free: false, currency: "INR", price_paise: 99900,
    original_price_paise: 149900, discounted_price_paise: 79900, effective_price_paise: 79900, status: "draft", display_status: "draft",
    launch_at: "2026-11-01T03:30:00Z", validity_value: 6, validity_unit: "months", validity_label: "6 months", published_at: null,
    archived_at: null, playlist_count: 1, lecture_count: 2, enrollment_count: 0, active_enrollment_count: 0, created_at: NOW,
    updated_at: NOW, version: 3, can_delete: true,
    playlists: [{ id: 11, title: "Full course", description: null, thumbnail: null, position: 0, chapters: [
      { id: 21, title: "Chapter 1", description: null, thumbnail: null, position: 0, lectures: [lecture(), lecture({ id: 32, title: "Ledgers", position: 1 })] },
      { id: 22, title: "Chapter 2", description: null, thumbnail: null, position: 1, lectures: [] },
    ] }],
    ...over,
  };
}

const ENROLLMENT: Enrollment = {
  id: 91, course_id: 7, course_title: "Class 12 Accountancy", student_id: "user_42", source: "payment",
  status: "active", payment_provider: "razorpay", payment_id: "pay_9", order_id: "order_9", currency: "INR", list_price_paise: 79900,
  amount_paid_paise: 59900, voucher_code: "SAVE200", purchased_at: "2026-11-10T05:30:00Z", access_start_at: "2026-11-10T05:30:00Z",
  validity_value: 6, validity_unit: "months", validity_label: "6 months", access_expires_at: "2027-05-10T05:30:00Z",
  status_reason: null, created_at: NOW, updated_at: NOW,
};

function useCourseApi(over: { course?: CourseDetail; list?: CourseDetail[] } = {}) {
  const detail = over.course ?? course();
  server.use(
    http.get(`${A}/summary`, () => HttpResponse.json({ total_courses: 4, published: 1, scheduled: 1, draft: 1, archived: 1, total_enrollments: 9,
      active_enrollments: 6, expired_enrollments: 2, pending_enrollments: 1, revenue: [{ currency: "INR", amount_paise: 479400 }] })),
    http.get(`${A}/taxonomy`, () => HttpResponse.json([
      { id: 1, kind: "class", name: "Class 12", sort_order: 0, is_active: true, course_count: 1 },
      { id: 2, kind: "stream", name: "Commerce", sort_order: 0, is_active: true, course_count: 1 },
      { id: 3, kind: "subject", name: "Accountancy", sort_order: 0, is_active: true, course_count: 1 },
    ])),
    http.get(`${A}/storage`, () => HttpResponse.json({ configured: false, max_video_mb: 4096, max_file_mb: 100, allowed_types: {} })),
    http.get(`${A}/enrollments`, () => HttpResponse.json(page([ENROLLMENT]))),
    http.get(`${A}/enrollments/:id`, () => HttpResponse.json({ ...ENROLLMENT, events: [
      { id: 1, event: "payment_confirmed", actor: "course_api", details: null, occurred_at: "2026-11-10T05:30:00Z" }] })),
    http.get(A, () => HttpResponse.json(page(over.list ?? [detail]))),
    http.get(`${A}/:id/publish-check`, () => HttpResponse.json({ ready: false, problems: [
      { code: "missing_launch_date", message: "Set the launch date.", blocking: true },
      { code: "missing_thumbnail", message: "Add a course thumbnail (recommended).", blocking: false }] })),
    http.get(`${A}/:id/preview`, () => HttpResponse.json({ ...detail, thumbnail_url: null, availability: { purchasable: true, reason: null, launch_at: detail.launch_at },
      playlists: [{ id: 11, title: "Full course", description: null, chapters: [{ id: 21, title: "Chapter 1", lectures: [
        { id: 31, title: "Journal entries", duration_seconds: 1800, is_free_preview: false, languages: ["english", "hindi"], resource_count: 0 }] }] }] })),
    http.get(`${A}/:id/preview/lectures/:lecture`, ({ request }) => {
      const lang = new URL(request.url).searchParams.get("language") ?? "english";
      return HttpResponse.json({ lecture_id: 31, title: "Journal entries", language: lang, language_name: lang === "hindi" ? "Hindi" : "English",
        available_languages: [{ code: "english", name: "English" }, { code: "hindi", name: "Hindi" }], video_url: `https://cdn.example.com/${lang}.mp4`,
        audio_url: null, subtitle_url: null, url_expires_in: null, free_preview: false, resources: [] });
    }),
    http.get(`${A}/:id`, () => HttpResponse.json(detail)),
  );
}

describe("course list", () => {
  it("shows summary cards and courses, and filters on the server", async () => {
    useCourseApi();
    const { user } = renderApp("/courses");
    expect(await screen.findByRole("heading", { name: "Course Management", level: 1 })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Total course revenue" })).toHaveTextContent("₹4,794.00");
    for (const label of ["Total courses", "Published courses", "Draft courses", "Archived courses", "Total enrollments", "Active enrollments", "Expired enrollments"])
      expect(screen.getByRole("region", { name: label })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Courses" });
    const row = within(table).getByRole("row", { name: /Class 12 Accountancy/ });
    expect(row).toHaveTextContent("Class 12 · Commerce · Accountancy");
    expect(row).toHaveTextContent("₹799.00");
    expect(row).toHaveTextContent("1 Nov 2026, 09:00 am IST");
    expect(row).toHaveTextContent("6 months");
    await user.type(screen.getByLabelText("Course name"), "account");
    await user.selectOptions(screen.getByLabelText("Status"), "scheduled");
    await user.selectOptions(screen.getByLabelText("Free / paid"), "paid");
    await user.selectOptions(screen.getByLabelText("Subject"), "3");
    await user.type(screen.getByLabelText("Launch from"), "2026-11-01");
    await user.click(screen.getByRole("button", { name: "Launch (IST)" }));
    await waitFor(() => expect(Object.fromEntries(last("GET", "/api/v1/courses").url.searchParams)).toMatchObject({
      q: "account", status: "scheduled", pricing: "paid", subject_id: "3", launch_from: "2026-11-01T00:00:00+05:30", sort: "launch_at", order: "asc",
    }));
  });

  it("validates the new-course form and sends IST dates and the validity preset", async () => {
    useCourseApi();
    server.use(http.post(A, async ({ request }) => HttpResponse.json(course({ id: 8, title: ((await request.json()) as { title: string }).title }), { status: 201 })));
    const { user } = renderApp("/courses");
    await user.click(await screen.findByRole("button", { name: "New course" }));
    const dialog = await screen.findByRole("dialog", { name: "New course" });
    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));
    expect(await within(dialog).findByText(/Enter a course name/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Enter a price above 0/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Course name"), "Economics 2027");
    await user.type(within(dialog).getByLabelText("Course price"), "999");
    await user.type(within(dialog).getByLabelText("Discounted price (optional)"), "999");
    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));
    expect(await within(dialog).findByText("Must be above 0 and below the course price.")).toBeInTheDocument();
    expect(requests.some((r) => r.method === "POST")).toBe(false);
    await user.clear(within(dialog).getByLabelText("Discounted price (optional)"));
    await user.type(within(dialog).getByLabelText("Discounted price (optional)"), "799");
    await user.selectOptions(within(dialog).getByLabelText("Class"), "1");
    await user.selectOptions(within(dialog).getByLabelText("Subject"), "3");
    await user.type(within(dialog).getByLabelText("Launch date and time (IST)"), "2026-11-15T10:00");
    await user.selectOptions(within(dialog).getByLabelText("Access validity"), "1y");
    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));
    expect(await screen.findByText("Draft “Economics 2027” created.")).toBeInTheDocument();
    expect(last("POST", "/api/v1/courses").body).toMatchObject({
      title: "Economics 2027", class_id: 1, subject_id: 3, stream_id: null, is_free: false, price_paise: 99900, discounted_price_paise: 79900,
      original_price_paise: null, launch_at: "2026-11-15T10:00:00+05:30", validity_value: 1, validity_unit: "years",
    });
    expect(last("POST", "/api/v1/courses").csrf).toBe(CSRF);
  });

  it("supports free courses and custom validity", async () => {
    useCourseApi();
    server.use(http.post(A, () => HttpResponse.json(course({ id: 9 }), { status: 201 })));
    const { user } = renderApp("/courses");
    await user.click(await screen.findByRole("button", { name: "New course" }));
    const dialog = await screen.findByRole("dialog", { name: "New course" });
    await user.type(within(dialog).getByLabelText("Course name"), "Free foundation");
    await user.click(within(dialog).getByRole("radio", { name: /Free/ }));
    expect(within(dialog).queryByLabelText("Course price")).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Access validity"), "custom");
    await user.clear(within(dialog).getByLabelText("Custom duration"));
    await user.type(within(dialog).getByLabelText("Custom duration"), "45");
    await user.selectOptions(within(dialog).getByLabelText("Duration unit"), "days");
    await user.click(within(dialog).getByRole("button", { name: "Create draft" }));
    await waitFor(() => expect(last("POST", "/api/v1/courses").body).toMatchObject({
      is_free: true, price_paise: null, discounted_price_paise: null, original_price_paise: null, validity_value: 45, validity_unit: "days",
    }));
  });

  it("asks before archiving and deleting, and is read-only for viewers", async () => {
    useCourseApi();
    server.use(http.post(`${A}/7/archive`, () => HttpResponse.json(course({ status: "archived", display_status: "archived" }))));
    const { user } = renderApp("/courses");
    await user.click(await screen.findByRole("button", { name: "Archive Class 12 Accountancy" }));
    const dialog = await screen.findByRole("dialog", { name: "Archive “Class 12 Accountancy”?" });
    expect(dialog).toHaveTextContent("keep access until their own expiry");
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(await screen.findByText("“Class 12 Accountancy” archived.")).toBeInTheDocument();
  });

  it("hides every write action from a viewer", async () => {
    useCourseApi();
    renderApp("/courses", "viewer");
    await screen.findByRole("table", { name: "Courses" });
    for (const name of [/New course/, /^Archive /, /^Delete /]) expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View Class 12 Accountancy" })).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    useCourseApi();
    const { container } = renderApp("/courses");
    await screen.findByRole("table", { name: "Courses" });
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("course editor", () => {
  it("edits details with the version and explains that existing students keep their validity", async () => {
    useCourseApi({ course: course({ enrollment_count: 3 }) });
    server.use(http.put(`${A}/7`, () => HttpResponse.json(course())));
    const { user } = renderApp("/courses/7");
    expect(await screen.findByRole("heading", { name: "Class 12 Accountancy", level: 1 })).toBeInTheDocument();
    expect(screen.getByText(/keep the validity and price they bought/)).toBeInTheDocument();
    expect(screen.getByLabelText("Launch date and time (IST)")).toHaveValue("2026-11-01T09:00");
    expect(screen.getByLabelText("Access validity")).toHaveValue("6m");
    await user.selectOptions(screen.getByLabelText("Access validity"), "10m");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Course saved.")).toBeInTheDocument();
    expect(last("PUT", "/api/v1/courses/7").body).toMatchObject({ version: 3, validity_value: 10, validity_unit: "months", launch_at: "2026-11-01T09:00:00+05:30" });
  });

  it("builds content: add a playlist, reorder lectures, delete a chapter", async () => {
    useCourseApi();
    server.use(
      http.post(`${A}/7/playlists`, () => HttpResponse.json({ id: 12, title: "Revision", description: null, thumbnail: null, position: 1, chapters: [] }, { status: 201 })),
      http.put(`${A}/chapters/21/lectures/order`, () => new HttpResponse(null, { status: 204 })),
      http.delete(`${A}/chapters/22`, () => new HttpResponse(null, { status: 204 })),
    );
    const { user } = renderApp("/courses/7?tab=content");
    await user.click(await screen.findByRole("button", { name: "Add playlist" }));
    const dialog = await screen.findByRole("dialog", { name: "Add playlist" });
    await user.type(within(dialog).getByLabelText("Playlist name"), "Revision");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByText("Playlist saved.");
    expect(last("POST", "/api/v1/courses/7/playlists").body).toEqual({ title: "Revision", description: null, thumbnail_asset_id: null });

    await user.click(screen.getByRole("button", { name: "Move lecture Journal entries down" }));
    await waitFor(() => expect(last("PUT", "/api/v1/courses/chapters/21/lectures/order").body).toEqual({ ids: [32, 31] }));
    expect(screen.getByRole("button", { name: "Move lecture Journal entries up" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Delete chapter Chapter 2" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete Chapter “Chapter 2”?" });
    expect(confirm).toHaveTextContent("enrollments and purchase history are not affected");
    await user.click(within(confirm).getByRole("button", { name: "Delete" }));
    await screen.findByText("Chapter “Chapter 2” deleted.");
  });

  it("adds a language version with a video URL and keeps languages unique", async () => {
    useCourseApi();
    server.use(http.post(`${A}/lectures/31/variants`, () => HttpResponse.json({ id: 42, language: "hindi", language_name: "Hindi" }, { status: 201 })));
    const { user } = renderApp("/courses/7?tab=content");
    await user.click(await screen.findByRole("button", { name: "Open lecture Journal entries" }));
    const drawer = await screen.findByRole("dialog", { name: "Lecture: Journal entries" });
    expect(within(drawer).getByRole("button", { name: "Preview English video" })).toBeInTheDocument();
    await user.click(within(drawer).getByRole("button", { name: "Add language version" }));
    const form = await screen.findByRole("dialog", { name: "Add language version" });
    expect(within(form).getByRole("option", { name: "English (already added)" })).toBeDisabled();
    await user.selectOptions(within(form).getByLabelText("Language"), "hindi");
    await user.click(within(form).getByRole("radio", { name: "Paste a video URL" }));
    await user.type(within(form).getByLabelText("Video URL"), "http://insecure");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByText("Enter an https:// video URL.")).toBeInTheDocument();
    await user.clear(within(form).getByLabelText("Video URL"));
    await user.type(within(form).getByLabelText("Video URL"), "https://cdn.example.com/hindi.mp4");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Hindi version saved.")).toBeInTheDocument();
    expect(last("POST", "/api/v1/courses/lectures/31/variants").body).toMatchObject({ language: "hindi", video_url: "https://cdn.example.com/hindi.mp4", video_asset_id: null });
  });

  it("refuses to publish a lecture without any video", async () => {
    useCourseApi({ course: course({ playlists: [{ id: 11, title: "Full course", description: null, thumbnail: null, position: 0, chapters: [
      { id: 21, title: "Chapter 1", description: null, thumbnail: null, position: 0, lectures: [lecture({ variants: [], status: "draft", default_language: null })] }] }] }) });
    const { user } = renderApp("/courses/7?tab=content");
    await user.click(await screen.findByRole("button", { name: "Open lecture Journal entries" }));
    const drawer = await screen.findByRole("dialog", { name: "Lecture: Journal entries" });
    await user.selectOptions(within(drawer).getByLabelText("Lecture status"), "published");
    await user.click(within(drawer).getByRole("button", { name: "Save lecture" }));
    expect(await within(drawer).findByText(/Add a video in at least one language/)).toBeInTheDocument();
    expect(requests.some((r) => r.method === "PUT")).toBe(false);
  });

  it("shows the publish check and publishes after confirmation", async () => {
    useCourseApi();
    server.use(http.post(`${A}/7/publish`, () => HttpResponse.json({ error: { code: "publish_requirements", message: "Set the launch date.", request_id: "r" } }, { status: 409 })));
    const { user } = renderApp("/courses/7?tab=publish");
    expect(await screen.findByText("1 item(s) must be fixed before publishing.")).toBeInTheDocument();
    expect(screen.getByText("Set the launch date.")).toBeInTheDocument();
    expect(screen.getByText("Add a course thumbnail (recommended).")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Publish" }));
    const confirm = await screen.findByRole("dialog", { name: "Publish this course?" });
    await user.click(within(confirm).getByRole("button", { name: "Publish" }));
    expect(await screen.findByText(/Set the launch date\. \(request r\)/)).toBeInTheDocument();
  });

  it("previews lectures with a language switch", async () => {
    useCourseApi();
    const { user } = renderApp("/courses/7?tab=preview");
    await user.click(await screen.findByRole("button", { name: "Play Journal entries" }));
    const language = await screen.findByLabelText("Language");
    expect(screen.getByText("Available in: English, Hindi")).toBeInTheDocument();
    await user.selectOptions(language, "hindi");
    await waitFor(() => expect(last("GET", "/api/v1/courses/7/preview/lectures/31").url.searchParams.get("language")).toBe("hindi"));
  });

  it("has no detectable accessibility violations in the content builder", async () => {
    useCourseApi();
    const { container } = renderApp("/courses/7?tab=content");
    await screen.findByRole("region", { name: "Course content" });
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("enrollments and taxonomy", () => {
  it("lists enrollments with the validity saved at purchase and the history", async () => {
    useCourseApi();
    server.use(http.post(`${A}/enrollments/91/cancel`, () => HttpResponse.json({ ...ENROLLMENT, status: "cancelled", events: [] })));
    const { user } = renderApp("/courses?tab=enrollments");
    const table = await screen.findByRole("table", { name: "Enrollments" });
    const row = within(table).getByRole("row", { name: /user_42/ });
    for (const text of ["user_42", "10 Nov 2026, 11:00 am IST", "Paid ₹599.00", "6 months", "10 May 2027, 11:00 am IST", "Active"])
      expect(row).toHaveTextContent(text);
    await user.selectOptions(screen.getByLabelText("Status"), "expired");
    await waitFor(() => expect(last("GET", "/api/v1/courses/enrollments").url.searchParams.get("status")).toBe("expired"));
    await user.click(within(row).getByRole("button", { name: "Details for user_42" }));
    const drawer = await screen.findByRole("dialog", { name: "Enrollment details" });
    expect(drawer).toHaveTextContent("₹599.00 of ₹799.00 (voucher SAVE200)");
    expect(drawer).toHaveTextContent("Payment confirmed: access started");
    expect(drawer).toHaveTextContent("Later changes to the course do not change them");
    await user.click(within(drawer).getByRole("button", { name: "Cancel enrollment" }));
    expect(await within(drawer).findByText(/Give a reason/)).toBeInTheDocument();
    await user.type(within(drawer).getByLabelText("Reason"), "chargeback");
    await user.click(within(drawer).getByRole("button", { name: "Cancel enrollment" }));
    await waitFor(() => expect(last("POST", "/api/v1/courses/enrollments/91/cancel").body).toEqual({ reason: "chargeback" }));
  });

  it("adds classes, streams and subjects", async () => {
    useCourseApi();
    server.use(http.post(`${A}/taxonomy`, () => HttpResponse.json({ id: 4, kind: "subject", name: "Economics", sort_order: 0, is_active: true, course_count: 0 }, { status: 201 })));
    const { user } = renderApp("/courses?tab=taxonomy");
    await user.type(await screen.findByLabelText("Add subject"), "Economics");
    await user.click(within(screen.getByLabelText("Add subject").closest("form")!).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(last("POST", "/api/v1/courses/taxonomy").body).toEqual({ kind: "subject", name: "Economics" }));
    expect(screen.getByRole("button", { name: "Delete Class 12" })).toBeDisabled(); // in use
  });
});

describe("money in paise", () => {
  it("converts rupee input to integer paise and back exactly", () => {
    expect(rupeesToPaise("999")).toBe(99900);
    expect(rupeesToPaise("0.1")).toBe(10);
    expect(rupeesToPaise("19.99")).toBe(1999);
    expect(rupeesToPaise("1.234")).toBeNull();
    expect(rupeesToPaise("abc")).toBeNull();
    expect(paiseToRupees(1999)).toBe("19.99");
    expect(paiseToRupees(5)).toBe("0.05");
    expect(paiseToRupees(null)).toBeNull();
  });
});

describe("file uploads", () => {
  it("asks for an upload slot under the course being edited, uploads to storage, then verifies", async () => {
    const asset = { id: 61, kind: "document", status: "pending", filename: "notes.pdf", content_type: "application/pdf", size_bytes: 4, url: null };
    server.use(
      http.post(`${A}/assets/uploads`, () =>
        HttpResponse.json({ asset, upload_url: "https://bucket.example.com/", fields: { key: "course-assets/7/61-notes.pdf" }, expires_in: 900 }, { status: 201 })),
      // The bucket's CORS rule must allow POST from the dashboard origin (preflight + upload).
      http.options("https://bucket.example.com/", () => new HttpResponse(null, {
        status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "*" },
      })),
      http.post("https://bucket.example.com/", () => new HttpResponse(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*" } })),
      http.post(`${A}/assets/61/complete`, () => HttpResponse.json({ ...asset, status: "uploaded" })),
    );
    const file = new File(["%PDF"], "notes.pdf", { type: "application/pdf" });
    const stored = await uploadToStorage("document", file, () => undefined, 7);
    expect(stored.status).toBe("uploaded");
    expect(last("POST", "/api/v1/courses/assets/uploads").body).toEqual({
      kind: "document", filename: "notes.pdf", content_type: "application/pdf", size_bytes: 4, course_id: 7,
    });
  });
});
