<template>
  <div class="p-6 bg-white rounded-2xl shadow">
    <h2 class="text-2xl font-bold mb-4">Gerador de Imagens para Vídeo</h2>

    <textarea
      v-model="text"
      class="w-full border rounded-xl p-4 min-h-[260px]"
      placeholder="Cole aqui a letra da música ou texto do vídeo..."
    ></textarea>

    <div class="flex items-center gap-4 mt-4">
      <label>Quantidade:</label>
      <select v-model="quantity" class="border rounded-lg p-2">
        <option :value="15">15 imagens</option>
        <option :value="16">16 imagens</option>
        <option :value="17">17 imagens</option>
        <option :value="18">18 imagens</option>
        <option :value="19">19 imagens</option>
        <option :value="20">20 imagens</option>
      </select>

      <button
        @click="generate"
        :disabled="loading"
        class="bg-blue-600 text-white px-5 py-2 rounded-xl disabled:opacity-50"
      >
        {{ loading ? "Gerando..." : "Gerar imagens" }}
      </button>
    </div>

    <p v-if="error" class="text-red-600 mt-4">{{ error }}</p>

    <div v-if="images.length" class="grid grid-cols-2 md:grid-cols-3 gap-4 mt-6">
      <div v-for="img in images" :key="img.url" class="border rounded-xl overflow-hidden">
        <img :src="img.url" class="w-full h-auto" />
        <div class="p-2 text-sm">Cena {{ img.scene }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";

const text = ref("");
const quantity = ref(15);
const loading = ref(false);
const error = ref("");
const images = ref([]);

async function generate() {
  error.value = "";
  images.value = [];

  if (!text.value || text.value.length < 20) {
    error.value = "Cole uma letra ou texto maior.";
    return;
  }

  loading.value = true;

  try {
    const res = await fetch("/api/storyboard/generate-images", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        text: text.value,
        quantity: quantity.value,
      }),
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Erro ao gerar imagens.");
    }

    images.value = data.images || [];
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}
</script>

