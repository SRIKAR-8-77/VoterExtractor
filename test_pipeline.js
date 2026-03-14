const fs = require('fs');

async function testPipeline() {
  const url = 'http://localhost:3000/api/process';
  
  console.log(`Hitting ${url} with the full pipeline fields...`);

  // Path to the demo PDF
  const pdfPath = './demo.pdf';
  if (!fs.existsSync(pdfPath)) {
    console.error("Please ensure demo.pdf exists in the current directory!");
    process.exit(1);
  }

  // Load the PDF content
  const fileBuffer = fs.readFileSync(pdfPath);
  const fileBlob = new Blob([fileBuffer], { type: 'application/pdf' });
  
  // Set up FormData
  const formData = new FormData();
  formData.append('files', fileBlob, 'demo.pdf');
  formData.append('localBodyId', '3');
  formData.append('prabhagNo', '1');
  formData.append('wardNo', '1');

  try {
    const response = await fetch(url, {
      method: 'POST',
      body: formData,
    });

    const data = await response.json();
    
    if (response.ok) {
      console.log('✅ Pipeline started successfully!');
      console.log('Response Details:', data);
      
      const jobId = data.job_id;
      console.log(`\nTo check the status, you can hit:`);
      console.log(`http://localhost:3000/api/progress/${jobId}`);
    } else {
      console.error('❌ Failed to start pipeline:', response.status);
      console.error('Error Details:', data);
    }
  } catch (error) {
    console.error('❌ Fetch Error:', error.message);
  }
}

testPipeline();
