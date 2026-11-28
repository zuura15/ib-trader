package com.ibtrader.launcher.proxy

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProxyConfigTest {
    @Test
    fun normalizesOriginOntoPort8000() {
        assertEquals("http://ib.tail.ts.net:8000", ProxyConfig.normalizeOrigin("ib.tail.ts.net"))
        assertEquals("http://ib.tail.ts.net:8000", ProxyConfig.normalizeOrigin("http://ib.tail.ts.net:8000/"))
        assertEquals("http://100.64.0.2:8000", ProxyConfig.normalizeOrigin("http://100.64.0.2"))
        assertEquals("http://ib.tail.ts.net:9000", ProxyConfig.normalizeOrigin("ib.tail.ts.net:9000"))
        assertEquals("https://ib.tail.ts.net:443", ProxyConfig.normalizeOrigin("https://ib.tail.ts.net:443"))
    }

    @Test
    fun rejectsBlankOrigin() {
        try {
            ProxyConfig.normalizeOrigin("  ")
            throw AssertionError("expected failure")
        } catch (err: IllegalArgumentException) {
            assertTrue(err.message!!.contains("empty"))
        }
    }

    @Test
    fun proxyRuleAndHeaders() {
        assertEquals("http://127.0.0.1:4321", ProxyConfig.proxyRule(4321))
        assertEquals(emptyMap<String, String>(), ProxyConfig.mainFrameHeaders("  "))
        assertEquals(mapOf("Authorization" to "Bearer secret"), ProxyConfig.mainFrameHeaders(" secret "))
    }

    @Test
    fun refusesToLoadOutsideTheTailnet() {
        assertEquals("Tailscale is not connected.", ProxyConfig.loadBlockReason(false, "http://h:8000", true))
        assertEquals("Set the IB Trader origin in Settings.", ProxyConfig.loadBlockReason(true, " ", true))
        assertTrue(ProxyConfig.loadBlockReason(true, "http://h:8000", false)!!.contains("proxy"))
        assertNull(ProxyConfig.loadBlockReason(true, "http://h:8000", true))
    }
}
