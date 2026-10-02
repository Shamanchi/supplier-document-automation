import { delay, http, HttpResponse } from "msw";

import { adminData, certificates, checks, products, suppliers } from "@/shared/mocks/db";

const api = "*/api";

function notFound(message = "Ресурс не найден") {
  return HttpResponse.json({ message }, { status: 404 });
}


export const handlers = [
  http.get(`${api}/checks`, async ({ request }) => {
    await delay(180);
    const url = new URL(request.url);
    const search = (url.searchParams.get("search") || "").toLowerCase();
    const status = url.searchParams.get("status") || "all";
    const date = url.searchParams.get("date") || "";
    const result = checks.filter((item) => {
      const matchesSearch =
        !search ||
        [item.tenderId, item.client, item.equipment]
          .join(" ")
          .toLowerCase()
          .includes(search);
      const matchesStatus = status === "all" || item.status === status;
      const matchesDate = !date || item.date.startsWith(date.split("-").reverse().join("."));
      return matchesSearch && matchesStatus && matchesDate;
    });
    return HttpResponse.json(result);
  }),

  http.get(`${api}/suppliers`, async () => {
    await delay(180);
    return HttpResponse.json(suppliers);
  }),

  http.post(`${api}/suppliers`, async ({ request }) => {
    await delay(220);
    const body = (await request.json()) as { name: string; email: string };
    const supplier = {
      ...structuredClone(suppliers[0]),
      id: `s-${Date.now()}`,
      name: body.name,
      products: [],
      files: [],
      discountRules: [],
      certificates: [],
    };
    suppliers.push(supplier);
    return HttpResponse.json(supplier, { status: 201 });
  }),

  http.post(
    `${api}/suppliers/:supplierId/files`,
    async ({ params, request }) => {
      await delay(260);
      const supplier = suppliers.find(
        (item) => item.id === String(params.supplierId),
      );
      if (!supplier) return notFound("Поставщик не найден");
      const body = (await request.json()) as {
        fileName: string;
        kind: "price" | "passport" | "specification" | "other";
      };
      supplier.files.push({
        id: `sf-${Date.now()}`,
        name: body.fileName,
        kind: body.kind,
        version: "v1",
        date: "26.07.2026",
        progress: 0,
        status: "pending",
      });
      return HttpResponse.json(supplier, { status: 201 });
    },
  ),

  http.get(`${api}/products`, async ({ request }) => {
    await delay(160);
    const url = new URL(request.url);
    const category = url.searchParams.get("category") || "all";
    const search = (url.searchParams.get("search") || "").toLowerCase();
    return HttpResponse.json(
      products.filter(
        (product) =>
          (category === "all" || product.category === category) &&
          (!search ||
            [
              product.sku,
              product.name,
              product.specifications,
              product.supplier,
            ]
              .join(" ")
              .toLowerCase()
              .includes(search)),
      ),
    );
  }),

  http.get(`${api}/certificates`, async () => {
    await delay(150);
    return HttpResponse.json(certificates);
  }),

  http.post(`${api}/certificates`, async ({ request }) => {
    await delay(240);
    const body = (await request.json()) as {
      supplier: string;
      product: string;
      number: string;
    };
    const certificate = {
      id: `cert-${Date.now()}`,
      supplier: body.supplier,
      product: body.product,
      number: body.number,
      status: "active" as const,
      validUntil: "26.07.2027",
      parameters: [],
    };
    certificates.push(certificate);
    return HttpResponse.json(certificate, { status: 201 });
  }),

  http.post(`${api}/dictionary/synonyms`, async ({ request }) => {
    await delay(200);
    const body = (await request.json()) as {
      canonical: string;
      synonym: string;
    };
    adminData.synonyms.push({
      id: `as-${Date.now()}`,
      synonym: body.synonym,
      canonical: body.canonical,
      author: "Текущий пользователь",
      similarity: 75,
      status: "pending",
    });
    return HttpResponse.json({ status: "pending" }, { status: 201 });
  }),

  http.get(`${api}/admin`, async () => {
    await delay(150);
    return HttpResponse.json(adminData);
  }),

  http.patch(`${api}/admin/settings`, async ({ request }) => {
    await delay(180);
    Object.assign(adminData.settings, await request.json());
    return HttpResponse.json(adminData);
  }),

  http.patch(
    `${api}/admin/synonyms/:synonymId`,
    async ({ params, request }) => {
      await delay(180);
      const synonym = adminData.synonyms.find(
        (item) => item.id === String(params.synonymId),
      );
      if (!synonym) return notFound("Синоним не найден");
      const body = (await request.json()) as {
        status: "accepted" | "rejected";
      };
      synonym.status = body.status;
      return HttpResponse.json(adminData);
    },
  ),
];
